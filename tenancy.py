"""
一個後端服務同時服務好幾家店（多店家）。

## 隔離靠的是什麼

每家店仍然是自己的資料庫（自己的 Neon 專案）。這個服務依網址前綴
/v/<店家識別> 決定這一個請求連哪一家的資料庫（見 app.py 的 _select_tenant）。

登入憑證（session token）存在各店自己的資料庫裡，所以 A 店的 token 拿到
B 店，B 店的資料庫裡查不到它，一定是 401。**這是越權的真正防線。**

會打穿這道防線的，是「存在記憶體、所有請求共用」的東西：快取、計數、旗標。
key 裡沒有店家的話，A 店請求留下的東西就會被 B 店的請求讀到——例如 token →
email 的快取命中，就根本不會去 B 店的資料庫查 token，直接沿用 A 店的身分。

★ **規則：行程內的狀態一律放進這裡的 PerTenant / TableCache，不要在模組層
  自己開 dict、也不要用 global 重新指派。** 它們會自己從「這個請求是哪一家
  店」取出那一家的那一份，呼叫端不用（也沒辦法）傳錯店家。

  tests/test_tenant_state.py 會掃整個後端：出現沒走這裡的模組層狀態，測試
  直接失敗；tests/test_cross_tenant.py 用兩家測試店驗證結果真的分得開。
  兩者都是 known-issue-check 清單上的固定檢查項目。

★ 不知道是哪一家店的時候，PerTenant 與 pool.connection() 會直接拋 NoTenant，
  **不會**退回某個預設的店。退回預設值的寫法在只有一家店的 demo 上永遠測不
  出問題，上線到多店家才會把資料混在一起。

## 設定的兩種寫法

單店（一個服務只有一家店，demo 就是這樣）：

    VARIANT=tutor-arthur-demo
    DATABASE_URL=...
    GOOGLE_CLIENT_ID=...          （以及 ALLOWED_ORIGINS、JOB_TOKEN、LINE_*）

多店家（共用的那個服務）：

    TENANTS=eli,tutor-arthur
    ELI__DATABASE_URL=...
    ELI__GOOGLE_CLIENT_ID=...
    TUTOR_ARTHUR__DATABASE_URL=...
    ...

店家識別轉成前綴的規則：轉大寫、連字號換成底線、後面接兩個底線。

兩種寫法讀出來是同一個結構（一份店家清單），之後的程式完全一樣——單店只是
清單裡剛好一家。所以 demo 跑的就是共用服務跑的那份程式。

★ 兩種寫法混用（同時設 TENANTS 與 VARIANT，或多店家時還留著沒前綴的
  DATABASE_URL）一律起不來。「沒前綴的那個是給誰的」沒有正確答案，猜錯
  的後果是某家店連到別家的資料庫，所以不猜。
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Generic, Iterator, Mapping, TypeVar

T = TypeVar("T")

# 店家識別的形狀：小寫英數字，用單一連字號分段。它會出現在網址與環境變數
# 名稱裡，限制成這樣才能確定轉換是一對一的（見 env_prefix）。
_ID_SHAPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# 每家店各自的設定。多店家寫法時要寫成 <前綴><鍵>，單店寫法直接用鍵名。
#
# ★ BREVO_API_KEY 刻意不在這裡：所有店共用一個 Brevo 帳號（封鎖名單是按
#   寄件人分開記的，2026-09-02 實測過），寄件人存在各店自己的 settings 表。
PER_TENANT_KEYS = (
    "DATABASE_URL",
    "GOOGLE_CLIENT_ID",
    "ALLOWED_ORIGINS",
    "JOB_TOKEN",
    "LINE_CHANNEL_SECRET",
    "LINE_CHANNEL_ACCESS_TOKEN",
)

# 允許的前端來源形狀：scheme + 主機（可帶埠號），不能有路徑、不能有結尾斜線。
# 寫錯的話 CORS 永遠對不上，而症狀只是瀏覽器主控台的一行跨來源錯誤——
# 在啟動時就擋下來，錯誤訊息直接講是哪一個值。
_ORIGIN_SHAPE = re.compile(r"^https?://[^/\s]+$")

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """店家設定寫錯。服務起不來，訊息會說是哪一個環境變數。"""


class NoTenant(RuntimeError):
    """這段程式需要知道是哪一家店，但現在沒有店家。

    在請求裡出現代表那支路由沒經過 /v/<店家> 前綴；在背景執行緒或腳本裡
    出現代表少了 `with tenancy.use(...)`。兩者都是程式錯誤，不是使用者的錯。
    """


@dataclass(frozen=True)
class Tenant:
    """一家店的設定。建好之後不能改（frozen），整個行程共用同一份。

    ★ 秘密欄位都設 repr=False。這個物件可能出現在 log 或例外訊息裡，
      repr 印出資料庫連線字串就等於把密碼寫進 log。
    """

    id: str
    database_url: str = field(repr=False)
    google_client_id: str = ""
    allowed_origins: tuple[str, ...] = ()
    job_token: str = field(default="", repr=False)
    line_channel_secret: str = field(default="", repr=False)
    line_channel_access_token: str = field(default="", repr=False)

    @property
    def line_enabled(self) -> bool:
        """兩個 LINE 憑證都有才算開。缺一個就整包停用（webhook 回 503）。"""
        return bool(self.line_channel_secret and self.line_channel_access_token)


def env_prefix(tenant_id: str) -> str:
    """店家識別 → 環境變數前綴。tutor-arthur → TUTOR_ARTHUR__"""
    return tenant_id.upper().replace("-", "_") + "__"


def _check_id(tenant_id: str, source: str) -> None:
    if not _ID_SHAPE.match(tenant_id):
        raise ConfigError(
            f"{source} 裡的店家識別「{tenant_id}」格式不對：只能用小寫英數字與連字號，"
            f"例如 eli、tutor-arthur。"
        )


def _parse_origins(raw: str, key: str) -> tuple[str, ...]:
    origins = tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())
    for o in origins:
        if not _ORIGIN_SHAPE.match(o):
            raise ConfigError(
                f"{key} 裡的「{o}」不是合法的來源。要寫成 https://主機（可帶埠號），"
                f"不要路徑，例如 https://eli.boo-king-king.workers.dev"
            )
    return origins


def _build(tenant_id: str, get: Callable[[str], str], *, prefix: str, multi: bool) -> Tenant:
    database_url = get("DATABASE_URL")
    if not database_url:
        raise ConfigError(f"{prefix}DATABASE_URL 沒有設定（店家 {tenant_id}）。")
    origins = _parse_origins(get("ALLOWED_ORIGINS"), f"{prefix}ALLOWED_ORIGINS")
    # ★ 多店家時來源是必填。CORS 放行的是所有店家來源的聯集，「這家店沒設」
    #   沒有安全的解讀——當成全部放行，就是任何網站都能替這家店的使用者打 API。
    #   單店寫法沿用原本的行為：沒設就全部放行並在啟動時警告（本機開發用）。
    if multi and not origins:
        raise ConfigError(
            f"{prefix}ALLOWED_ORIGINS 沒有設定（店家 {tenant_id}）。"
            f"多店家時每家店都要設自己的前端網址。"
        )
    return Tenant(
        id=tenant_id,
        database_url=database_url,
        google_client_id=get("GOOGLE_CLIENT_ID"),
        allowed_origins=origins,
        job_token=get("JOB_TOKEN"),
        line_channel_secret=get("LINE_CHANNEL_SECRET"),
        line_channel_access_token=get("LINE_CHANNEL_ACCESS_TOKEN"),
    )


def load(environ: Mapping[str, str]) -> dict[str, Tenant]:
    """從環境變數讀出店家清單。寫錯一律拋 ConfigError，不猜。

    純函式（不碰模組層狀態），測試直接餵 dict 進來。
    """
    tenants_raw = environ.get("TENANTS", "").strip()
    variant = environ.get("VARIANT", "").strip()

    if tenants_raw and variant:
        raise ConfigError(
            "TENANTS 與 VARIANT 只能設一個。多店家用 TENANTS（加上各店的前綴設定），"
            "單店用 VARIANT。"
        )

    if tenants_raw:
        ids = [x.strip() for x in tenants_raw.split(",") if x.strip()]
        if not ids:
            raise ConfigError("TENANTS 是空的。")
        if len(set(ids)) != len(ids):
            raise ConfigError(f"TENANTS 裡有重複的店家：{tenants_raw}")

        leftovers = [k for k in PER_TENANT_KEYS if environ.get(k, "").strip()]
        if leftovers:
            raise ConfigError(
                "多店家設定（TENANTS）下不能有沒前綴的 "
                + "、".join(leftovers)
                + "：它是給哪一家店的沒有正確答案。請改成 <店家前綴>__<鍵>，"
                "例如 ELI__DATABASE_URL。"
            )

        prefixes: dict[str, str] = {}
        tenants: dict[str, Tenant] = {}
        for tid in ids:
            _check_id(tid, "TENANTS")
            prefix = env_prefix(tid)
            prefixes[prefix] = tid
            tenants[tid] = _build(
                tid, lambda k, p=prefix: environ.get(p + k, "").strip(),
                prefix=prefix, multi=True,
            )

        # 拼錯的鍵名（ELI__DATABSE_URL）或漏加進 TENANTS 的店家，都會變成一個
        # 沒人讀的環境變數——設定看起來有填，服務卻當作沒有。在這裡講出來。
        for key in environ:
            if "__" not in key:
                continue
            head, _, tail = key.partition("__")
            prefix = head + "__"
            if prefix in prefixes:
                if tail not in PER_TENANT_KEYS:
                    raise ConfigError(
                        f"不認得的店家設定 {key}。可以用的鍵：{'、'.join(PER_TENANT_KEYS)}。"
                    )
            elif tail in PER_TENANT_KEYS:
                raise ConfigError(
                    f"{key} 看起來是某家店的設定，但 {head} 不在 TENANTS 裡"
                    f"（TENANTS={tenants_raw}）。"
                )
        return tenants

    if variant:
        _check_id(variant, "VARIANT")
        return {
            variant: _build(
                variant, lambda k: environ.get(k, "").strip(), prefix="", multi=False
            )
        }

    raise ConfigError(
        "沒有店家設定。單店請設 VARIANT（例如 tutor-arthur-demo）與 DATABASE_URL；"
        "多店家請設 TENANTS（例如 eli,tutor-arthur）與各店的 <前綴>__DATABASE_URL。"
    )


# 整個行程的店家清單。第一次用到時讀一次環境變數，之後唯讀（MappingProxyType
# 擋住任何寫入）。app.py 一 import 就會用到它，所以設定寫錯仍然是「啟動就失敗」。
#
# ★ 不在 import 時就讀，是為了讓 load() 能單獨測試：import 這個模組本身
#   不該需要一份真的店家設定。
_registry: Mapping[str, Tenant] | None = None
_registry_lock = threading.Lock()


def _tenants() -> Mapping[str, Tenant]:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MappingProxyType(load(os.environ))
    return _registry

# 「這個請求是哪一家店」。每個請求開頭由 app.py 設定、結束時還原。
#
# ★ 用 ContextVar 而不是 flask.g：背景執行緒、啟動程序、命令列腳本都不在
#   Flask 的請求裡，但一樣需要知道店家（用 `with tenancy.use(...)`）。
#   ContextVar 每條執行緒各自一份，gunicorn 的 threads 之間不會互相看到。
_current: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar(
    "boo_king_king_tenant", default=None
)


def get(tenant_id: str) -> Tenant | None:
    """依識別找店家。找不到回 None。"""
    return _tenants().get(tenant_id)


def all_tenants() -> tuple[Tenant, ...]:
    return tuple(_tenants().values())


def current() -> Tenant:
    """這個請求是哪一家店。沒有就拋 NoTenant——不要退回預設值，理由見檔案開頭。"""
    tenant = _current.get()
    if tenant is None:
        raise NoTenant(
            "這段程式需要知道是哪一家店，但現在沒有店家。請求要帶 /v/<店家> 前綴；"
            "背景工作與腳本要包在 `with tenancy.use(...)` 裡。"
        )
    return tenant


def current_or_none() -> Tenant | None:
    """給「沒有店家也合理」的地方用（例如 /api/health、log 標記）。"""
    return _current.get()


def activate(tenant: Tenant | None) -> contextvars.Token:
    """設定目前的店家，回傳還原用的 token。給 app.py 的請求開頭／結尾用。"""
    return _current.set(tenant)


def deactivate(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def use(tenant: Tenant | str) -> Iterator[Tenant]:
    """在這個區塊裡，目前的店家是 tenant。區塊結束一定還原。"""
    if isinstance(tenant, str):
        found = get(tenant)
        if found is None:
            raise NoTenant(f"沒有「{tenant}」這家店。")
        tenant = found
    token = _current.set(tenant)
    try:
        yield tenant
    finally:
        _current.reset(token)


def for_cli(environ: Mapping[str, str] = os.environ) -> Tenant:
    """命令列腳本要對哪一家店做事。

    只有一家店就是那一家；有好幾家時一定要用環境變數 TENANT=<店家> 指定。
    ★ 不挑第一家當預設——對著錯的店家跑遷移或塞測試資料，事後很難發現。
    """
    tenants = _tenants()
    wanted = environ.get("TENANT", "").strip()
    if wanted:
        tenant = tenants.get(wanted)
        if tenant is None:
            raise NoTenant(f"TENANT={wanted} 不在店家清單裡（{', '.join(tenants)}）。")
        return tenant
    if len(tenants) == 1:
        return next(iter(tenants.values()))
    raise NoTenant(
        f"有 {len(tenants)} 家店（{', '.join(tenants)}），請用 TENANT=<店家> 指定要對哪一家做。"
    )


class PerTenant(Generic[T]):
    """每家店一份的行程內狀態。

        _hits = tenancy.PerTenant(dict)

        def check(user_id):
            hits = _hits.get()          # 目前這家店的那一份
            ...

    第一次讀到某家店時用 factory() 建一份新的。get() 從目前的店家取，所以
    呼叫端根本沒有傳錯店家的機會；不在任何店家裡呼叫 get() 會拋 NoTenant。

    ★ 這裡只保證「每家店一份」，不保證那一份的執行緒安全——裡面的 dict
      有沒有被多條執行緒同時改，仍然要呼叫端自己用鎖（跟原本一樣）。
    """

    def __init__(self, factory: Callable[[], T]):
        self._factory = factory
        self._by_tenant: dict[str, T] = {}
        self._lock = threading.Lock()

    def get(self) -> T:
        return self.for_tenant(current().id)

    def for_tenant(self, tenant_id: str) -> T:
        """指定店家取那一份。只給沒有請求情境的地方用（背景執行緒依訊息上的
        店家記帳）。一般程式用 get()。"""
        with self._lock:
            value = self._by_tenant.get(tenant_id)
            if value is None:
                value = self._factory()
                self._by_tenant[tenant_id] = value
            return value

    def reset(self) -> None:
        """全部店家都清掉。只給測試用。"""
        with self._lock:
            self._by_tenant.clear()


class _Entry:
    """TableCache 的一格：值與到期時間。"""

    __slots__ = ("value", "expires_at")

    def __init__(self) -> None:
        self.value: Any = None
        self.expires_at = 0.0


class TableCache(Generic[T]):
    """一張小設定表的快取（functions、roles、settings 這種幾列、幾乎不變的）。
    每家店一份，TTL 到了就重讀。

        _cache = tenancy.TableCache(600, _load)      # _load(cur) -> 值
        value = _cache.get(cur)
        _cache.invalidate()                          # 目前這家店的作廢

    ★ 查資料庫刻意放在鎖外面。鎖只保護那一格，包住網路操作的話，一次連線
      變慢就會讓所有等著讀設定的請求一起卡住（原本 db/roles.py 的理由）。
    """

    def __init__(self, ttl_seconds: float, loader: Callable[[Any], T]):
        self._ttl = ttl_seconds
        self._loader = loader
        self._entries: PerTenant[_Entry] = PerTenant(_Entry)
        self._lock = threading.Lock()

    def get(self, cur) -> T:
        entry = self._entries.get()
        with self._lock:
            if entry.value is not None and entry.expires_at > time.monotonic():
                return entry.value
        loaded = self._loader(cur)
        with self._lock:
            entry.value = loaded
            entry.expires_at = time.monotonic() + self._ttl
        return loaded

    def invalidate(self) -> None:
        entry = self._entries.get()
        with self._lock:
            entry.value = None
            entry.expires_at = 0.0

    def reset(self) -> None:
        """全部店家都清掉。只給測試用。"""
        self._entries.reset()


class LogContext(logging.Filter):
    """在每一行 log 標上店家（record.tenant）。沒有店家時是「-」。

    多家店共用一份 log，出事時要分得出是哪一家——格式裡放 %(tenant)s 就好。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tenant = _current.get()
        record.tenant = tenant.id if tenant else "-"
        return True
