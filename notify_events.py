"""五個觸發點：把「發生了什麼事」翻成「要寄哪封信給誰」。

跟 notifications.py 分開，是因為兩者變動的理由不同：那支是寄信的機制
（佇列、逾時、重試），這支是業務規則（誰該被通知）。改「主持人確認後也要
副知玩家」不該碰到寄信機制，反之亦然。

## 這裡的每一支都遵守同一個形狀

    def on_xxx(cur, ...) -> Message | list | None

**只組信，不寄信。** 呼叫端在 commit 之後才呼叫 notifications.dispatch()。
理由見 notifications.py 開頭——rollback 掉的預約不能留下一封「預約成功」。

## 為什麼整支包在 try 裡

`safe()` 把每一支都包成「出事就回 None」。組信會讀資料庫、格式化日期、
代入變數，任何一步都可能因為一個沒想到的 None 而爆掉——而那不該讓一筆
已經成立的預約回 500。少一封通知是小事，預約失敗是大事。
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime

import notifications as nf
from db import functions

log = logging.getLogger(__name__)

_WEEKDAY = "一二三四五六日"


def safe(fn):
    """組信失敗就當作沒有信要寄，**而且不能弄髒呼叫端的交易**。

    ## 為什麼光是 try/except 不夠（2026-09-02 實測）

    這些函式跑在呼叫端的交易裡（要 cursor 才能讀範本與收件人），而 PostgreSQL
    的交易一旦有任何一句 SQL 出錯就會進入 aborted 狀態。在那之後：

        conn.commit()   # 不會拋例外，但 Postgres 把它當成 ROLLBACK

    所以只用 try/except 把例外吞掉的話，會變成**最糟的那種壞法**：組信時有一句
    SQL 爆掉 → 例外被吞掉 → commit 悄悄變成 rollback → API 回 201「預約成功」
    → 而那筆預約根本不存在。使用者不會收到任何錯誤，只會在之後發現場次不見了。

    實測（scratchpad/poison.py）：讓 _gm_names 裡的查詢失敗，同一筆交易中先寫進去
    的 note 就消失了，而 commit() 一聲不吭。

    ## 做法

    每一支都包在 SAVEPOINT 裡。組信失敗就 ROLLBACK TO SAVEPOINT，交易回到呼叫
    這支之前的乾淨狀態，呼叫端的寫入完全不受影響。這正是 savepoint 存在的理由：
    讓交易裡的一小段可以獨立失敗。

    ★ 第一個位置參數必須是 cursor——這裡的每一支都是這個形狀。
    """
    @functools.wraps(fn)
    def wrapper(cur, *args, **kwargs):
        if not nf.enabled():
            return None
        try:
            cur.execute("SAVEPOINT notify")
        except Exception as exc:  # noqa: BLE001 - 連 savepoint 都下不了就別碰交易
            log.warning("通知：無法建立 savepoint，略過 %s: %s", fn.__name__, exc)
            return None
        try:
            result = fn(cur, *args, **kwargs)
            cur.execute("RELEASE SAVEPOINT notify")
            return result
        except Exception as exc:  # noqa: BLE001
            log.warning("組通知信失敗 %s: %s", fn.__name__, exc)
            try:
                cur.execute("ROLLBACK TO SAVEPOINT notify")
            except Exception:  # noqa: BLE001 - 連線本身壞了，交易反正保不住
                pass
            return None
    return wrapper


# ── 格式化 ──────────────────────────────────────────────────────

def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def session_text(session_date, session_time) -> tuple[str, str]:
    """回傳 (完整, 精簡)：('2026-09-05（六）19:00', '2026/9/5 19:00')。

    完整版給內文——收件人要能一眼確定是哪一天，星期幾是最容易對到自己
    行事曆的資訊。精簡版給主旨,省掉星期。

    ★ 年份**要留**。原本省掉了，理由是「信是當下寄的，年份不會有歧義」——
      那是錯的：場次可以訂在很久以後，主旨只寫「12/31」時收件人分不出是
      今年還是明年，而主旨正是他之後在信箱搜尋時看到的那一行。星期可以省
      （內文有），年份不行。
    """
    d = _as_date(session_date)
    t = str(session_time or "")[:5]
    if d is None:
        return f"{session_date} {t}".strip(), f"{session_date} {t}".strip()
    return (
        f"{d.isoformat()}（{_WEEKDAY[d.weekday()]}）{t}",
        f"{d.year}/{d.month}/{d.day} {t}",
    )


def _gm_names(cur, gm_user_ids: list[int | None]) -> str:
    """已指派的主持人名字，逗號分隔。沒有人就回「未指派」。"""
    ids = [i for i in (gm_user_ids or []) if i]
    if not ids:
        return "未指派"
    cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", (ids,))
    by_id = dict(cur.fetchall())
    # 依 gm1..gm4 的順序而不是資料庫回傳的順序——那是劇本定義的角色順序，
    # 主持人自己看得懂哪個是哪個。
    return "、".join(by_id.get(i, f"#{i}") for i in ids)


def _gm_role_name(cur, mmg_id: int, gm_user_ids: list, gm_id: int) -> str:
    """這位主持人在這個劇本裡擔任的角色名稱（gm1_name..gm4_name）。"""
    try:
        slot = [i for i in gm_user_ids].index(gm_id) + 1
    except ValueError:
        return ""
    cur.execute(f"SELECT gm{slot}_name FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    return (row[0] if row else "") or f"主持 {slot}"


def _booking_values(cur, booking: dict) -> dict:
    full, short = session_text(booking["session_date"], booking["session_time"])
    return {
        "mmg_name": booking["mmg_name"],
        "session": full,
        "session_short": short,
        "player_name": booking["player_name"],
        "gm_list": _gm_names(cur, booking["gm_user_ids"]),
        "deposit": (
            f"{booking['booking_cost']} 元"
            if booking.get("booking_cost")
            else "依現場規定"
        ),
    }


# ── ①②③ 預約的狀態變化 ────────────────────────────────────────
#
# 三封預約通知都由**同一支**函式決定，比對「動作前」與「動作後」的預約。
# 這樣寫而不是在四個路由各自插一段，是因為同一個狀態可以從好幾條路到達：
# booked 可以是玩家付了訂金自動轉的，也可以是管理員在後台直接改的；
# gm_reviewed 可以是最後一位主持人按了確認，也可以是管理員勾了確認欄。
# 每條路各插一段的話，之後新增第五條路就會安靜地不發通知——而「信沒寄」
# 這種錯不會有人回報，只會有人覺得系統怪怪的。
#
# ★ before 是 None 代表「這筆剛被建立」。

_DEAD = ("cancelled", "ended")


@safe
def on_transition(cur, before: dict | None, after: dict | None) -> list:
    """比對前後狀態，回傳這次該寄的所有信。"""
    if not after or after["status"] in _DEAD:
        return []

    out = []
    old_status = before["status"] if before else None
    old_gms = set(i for i in (before["gm_user_ids"] if before else []) if i)
    new_gms = [i for i in after["gm_user_ids"] if i and i not in old_gms]
    base = _booking_values(cur, after)

    # ① 這次新被指定到的主持人。已經被指定過的不重寄——管理員為了改備註
    #    存一次檔，不該讓所有主持人再收一封「你被指定了」。
    #
    # ★ single 模式整段跳過。那種店的主持人不是「被指定」的，是系統依組態
    #   自動填上去的店主本人——她每收到一筆預約就收一封「你被指定為主持人，
    #   請前往確認」，而信裡的按鈕連到的主持人介面在那種店根本不存在
    #   （前端沒有那個畫面）。
    #
    #   她真正需要的那封是 ③「預約成立」，那封本來就會寄給訂場的玩家；
    #   店家要知道有新預約的話，是另一件事、要另一種信，不是把一封講錯
    #   對象的信留著充數。
    if functions.gm_mode(cur) != "single":
        for gm in nf.users_by_id(cur, new_gms):
            msg = nf.build(cur, "gm_assigned", [gm], {
                **base,
                "gm_name": gm["name"] or "主持人",
                "gm_role": _gm_role_name(
                    cur, after["mmg_id"], after["gm_user_ids"], gm["id"]
                ),
            })
            if msg:
                out.append(msg)

    # ② 主持人全數確認 → 通知管理員去收訂金。
    if after["status"] == "gm_reviewed" != old_status:
        msg = nf.build(cur, "admin_reviewed", nf.admins(cur), base)
        if msg:
            out.append(msg)

    # ③ 預約成立 → 通知玩家。唯一一封寄給一般玩家的信，也是唯一一封
    #    結尾寫「（請勿回覆）」的：其他四封的收件人都是店內人員。
    if after["status"] == "booked" != old_status:
        player = nf.users_by_id(cur, [after["player_id"]])
        msg = nf.build(cur, "player_booked", player, base)
        if msg:
            out.append(msg)

    return out


# ── ④⑤ 異常記點與停權 ──────────────────────────────────────────

_KIND_LABEL = {
    "bad_request": "異常請求",
    "rate_limit": "請求過於頻繁",
}


def _rules_text(cur) -> str:
    cur.execute("SELECT min_point, ban_seconds FROM abuse_rule ORDER BY min_point")
    parts = []
    for min_point, secs in cur.fetchall():
        parts.append(f"{min_point} 點停權 {_duration(secs)}")
    return "、".join(parts) if parts else "未設定"


def _duration(seconds: int) -> str:
    if not seconds:
        return "無限期"
    for unit, label in ((86400, "天"), (3600, "小時"), (60, "分鐘")):
        if seconds >= unit and seconds % unit == 0:
            return f"{seconds // unit} {label}"
    return f"{seconds} 秒"


def _until_text(until) -> tuple[str, str]:
    """(完整, 主旨用的短版)。until 是 None 代表無限期。"""
    if until is None:
        return "無解除期限（需管理員手動解除）", "（無期限）"
    # 主旨同樣帶年份，理由與 session_text 相同——同一套規則，不要一封有
    # 一封沒有。
    return (
        f"{until.year}-{until.month:02d}-{until.day:02d} {until.strftime('%H:%M')}",
        f"至 {until.year}/{until.month}/{until.day}",
    )


@safe
def on_abuse(cur, user_id: int, kind: str, outcome: dict):
    """寄給管理員。outcome 是 guard.record() 的回傳。

    ★ **跨過停權門檻的那一次只發 ⑤，不發 ④。**（案主決定）同一個事件
      同時寄「他被記點了」跟「他被停權了」兩封，第一封講的事第二封全都
      包含了，只是製造兩倍的信。
    """
    if not outcome:
        return None
    cur.execute("SELECT name, email FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    name, email = row
    to = nf.admins(cur)
    base = {
        "user_name": name or f"#{user_id}",
        "user_email": email or "",
        "kind": _KIND_LABEL.get(kind, kind),
        "total": outcome["total"],
    }

    ban = outcome.get("ban")
    if ban:
        until, until_short = _until_text(ban.get("until"))
        return nf.build(cur, "abuse_banned", to, {
            **base, "until": until, "until_short": until_short,
        })

    now = datetime.now()
    return nf.build(cur, "abuse_point", to, {
        **base,
        "added": outcome["added"],
        "at": now.strftime("%Y-%m-%d %H:%M"),
        "rules": _rules_text(cur),
    })
