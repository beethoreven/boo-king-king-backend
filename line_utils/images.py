"""LINE 的查詢指令回一張圖：要畫什麼、網址怎麼簽、來抓的時候怎麼畫。

    指令 → ImageReply（要畫哪一天／哪幾個月）→ 簽名網址 → 回覆給 LINE
    LINE 照網址來抓 → 驗簽 → 查資料 → draw 畫成 PNG

## 為什麼回的是網址

LINE 的圖片訊息**只收網址**（HTTPS），不收檔案本身。所以 bot 回的是一個
指向這個後端的網址，LINE 再來抓（路由在 app.py 的 /line/img/...）。

## 網址帶簽名

網址裡寫著要畫哪一天、哪幾個月，改一個數字就是另一天——沒有簽名的話，
拿到一張圖網址的人就能看任何一天。簽名用 LINE 的 channel secret 導出來的
鑰匙，只有這個後端簽得出來；權限在發網址之前就查過了（handler 的 is_staff），
網址本身就是那次查詢的憑證。

★ 網址裡還記著發出的時間。不拿它設期限（理由見最後一段）；留著是為了 log
  能算出「發出後幾秒被抓」。

## 來抓的時候才畫，不存檔

LINE 什麼時候來抓圖——發送當下，還是有人打開聊天室時——官方文件沒寫。
存檔的話就得決定「存多久」，而那正是不知道的事；存在記憶體裡則每次部署、
每次休眠都會清掉。所以每次來抓都照當下的資料現畫，圖的最下面印「截至幾點」，
看圖的人知道那是什麼時候的樣子。

★ 每抓一次，log 記一行 [line-img]：距離發出網址幾秒、誰來抓（User-Agent）、
  畫了多久。

## 實際上是誰、什麼時候來抓（2026-09-19 eli 第一次使用的 log）

    [line-img] m202608-202612 發出後 3 秒被抓｜畫圖 1869 ms｜35 KB｜UA: DESKTOP:MAC:…
    [line-img] m202608-202612 發出後 3 秒被抓｜畫圖 1778 ms｜35 KB｜UA: DESKTOP:MAC:…

- **來抓的是 LINE 的 Mac 版 App 本身**（UA），不是 LINE 的伺服器——LINE 沒有
  替我們把圖存起來，是看訊息的那台裝置自己來抓，而且訊息一出現就抓（3 秒）。
- 預覽圖與原圖是同一個網址，Mac 版**兩個都抓**，所以每次顯示會畫兩張。
- 畫五個月在 Render 上約 1.8 秒，本機純畫圖約 0.2 秒。這兩次可能各是一個
  worker 第一次畫圖，含載入 Pillow 的時間；之後的 log 會看得出來。
- 所以**網址不設期限**：之後才打開聊天室的裝置會在那時候才來抓，期限一到，
  晚看的人就是一張破圖。
- 手機版是不是也一樣（直接抓、打開時才抓），這次只有一台 Mac 的資料，要看
  之後出現的手機 UA 那幾行。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from admin_utils import bookings_admin
from db import busy as busy_db
from db.bookings import reachable_tabs

from line_utils import commands

# 台北是固定的 UTC+8、沒有日光節約（同 admin_utils/mmg_admin.py）。
_TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ImageReply:
    """handler 回給 app.py 的「這次要回一張圖」。

    handler 不碰 HTTP，不知道自己的網址長什麼樣，所以只講要畫什麼；
    網址由 app.py 在請求裡組（見 sign）。

    spec  d20260830           那一天的場次
          m202608-202612      2026/8 到 2026/12 的行事曆（頭尾都含）
    """
    spec: str

    @classmethod
    def day(cls, d: date) -> ImageReply:
        return cls(f"d{d:%Y%m%d}")

    @classmethod
    def months(cls, months: list[tuple[int, int]]) -> ImageReply:
        (fy, fm), (ly, lm) = months[0], months[-1]
        return cls(f"m{fy:04d}{fm:02d}-{ly:04d}{lm:02d}")


_DAY_SPEC = re.compile(r"d(\d{4})(\d{2})(\d{2})")
_MONTHS_SPEC = re.compile(r"m(\d{4})(\d{2})-(\d{4})(\d{2})")


def parse_spec(spec: str):
    """spec → ('day', date) 或 ('months', [(年, 月), ...])；看不懂就 None。

    ★ 簽名驗過的 spec 一定是這裡自己產的，照理不會看不懂。還是逐項檢查，
      是因為「簽名對」只證明是我們發的，不證明發的時候沒有 bug。
    """
    m = _DAY_SPEC.fullmatch(spec)
    if m:
        try:
            return "day", date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _MONTHS_SPEC.fullmatch(spec)
    if m:
        # 跟指令走同一支解析，範圍與上限的規則只有一份。
        kind, payload = commands.parse(
            f"{m.group(1)}/{m.group(2)}~{m.group(3)}/{m.group(4)}行事曆")
        if kind == commands.CALENDAR:
            return "months", payload["months"]
    return None


# ── 簽名 ─────────────────────────────────────────────────────────

_TOKEN = re.compile(r"([dm][0-9-]{8,15})\.([0-9]{1,12})\.([A-Za-z0-9_-]{22})")


def _key(secret: str) -> bytes:
    """從 channel secret 導出一把只給圖片網址用的鑰匙。

    同一把 secret 也拿來驗 webhook 的簽章。分開導出，兩種簽名就不可能互相
    冒用——就算哪天格式碰巧長得一樣。
    """
    return hmac.new(secret.encode(), b"boo-king-king/line-image", hashlib.sha256).digest()


def _mac(secret: str, body: str) -> str:
    raw = hmac.new(_key(secret), body.encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(spec: str, secret: str, *, issued: int | None = None) -> str:
    """spec → 放進網址的那一段：'d20260830.1758260000.<22 碼簽名>'。"""
    body = f"{spec}.{int(time.time()) if issued is None else issued}"
    return f"{body}.{_mac(secret, body)}"


def verify(token: str, secret: str) -> tuple[str, int] | None:
    """驗簽。對的話回 (spec, 發出的時間)，不對回 None。"""
    m = _TOKEN.fullmatch(token)
    if not m:
        return None
    spec, issued, sig = m.groups()
    if not hmac.compare_digest(sig, _mac(secret, f"{spec}.{issued}")):
        return None
    return spec, int(issued)


# ── 來抓的時候 ───────────────────────────────────────────────────

def render(cur, spec: str, *, issued: int, user_agent: str) -> bytes:
    """照當下的資料畫圖，並記一行 log（為什麼要記，見開頭）。

    資料跟網站的場次行事曆是同一份：同一支 month_days（點，不含已取消）、
    同一支 list_date（清單，全部列出）、同一組店家忙碌日、同一份圖例。
    """
    parsed = parse_spec(spec)
    if parsed is None:
        raise ValueError(f"看不懂的 spec：{spec}")
    kind, value = parsed
    started = time.perf_counter()
    # ★ 在這裡才 import：Pillow 只有開了 LINE 的店用得到。放在檔案開頭的話，
    #   沒開 LINE 的店（步經徑）每個 worker 開機都會把整套 Pillow 載進記憶體，
    #   而它一張圖都不會畫。每個 worker 第一次畫圖時多花的那一點時間算進 log
    #   的「畫圖」裡——那也是 LINE 等的時間。
    from line_utils import draw

    now = datetime.now(_TAIPEI)

    if kind == "day":
        page = bookings_admin.list_date(cur, value)
        png = draw.day_png(value, page["items"], page["has_more"], now=now)
    else:
        days: dict[str, list[str]] = {}
        busy: set[str] = set()
        for y, m in value:
            days.update(bookings_admin.month_days(cur, y, m))
            busy.update(busy_db.dates_in(cur, busy_db.STORE, None, y, m))
        # 圖例同網站：這家店到得了的狀態，已取消不列——它不畫點。
        legend = [(k, v) for k, v in reachable_tabs(cur).items() if k != "cancelled"]
        png = draw.calendar_png(value, days, busy, legend, now=now)

    print(
        f"[line-img] {spec} 發出後 {time.time() - issued:.0f} 秒被抓"
        f"｜畫圖 {(time.perf_counter() - started) * 1000:.0f} ms（含查資料）"
        f"｜{len(png) // 1024} KB｜UA: {user_agent[:120] or '(無)'}",
        flush=True,
    )
    return png
