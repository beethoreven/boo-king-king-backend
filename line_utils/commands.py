"""群組訊息 → 指令。

## 指令

    @bot 預訂 丹水山莊 2026/10/5 14:00     訂場
    @bot 2026/8/30場次                     那天的場次（回一張圖）
    @bot 2026/8行事曆                      那個月的行事曆（回一張圖）
    @bot 2026行事曆                        那一年十二個月
    @bot 2026/8~2026/12行事曆              這幾個月，最多 MAX_MONTHS 個

這幾種以外一律回「我不知道你在說什麼」。不做模糊比對、不猜意圖——
這支 bot 會真的寫入店家的營運資料，猜錯的代價是一筆不存在的預約，而
猜對省下的只是使用者少打兩個字。查詢不寫入，但規則跟著同一套：每一種查詢
都有自己的關鍵字（場次、行事曆），光打日期不算。查當天要加「場次」是案主
2026-09-19 定的：有關鍵字可以抓，資訊量就不會跟其他指令重疊。

## 年份必填（案主指定）

省略年份看似方便，但跨年那幾天會猜錯，而且錯得無聲：十二月底打「1/5」
到底是下個月還是十三個月後？兩種都合理，系統選哪一個都會有人被坑。
要求四位數年份，一次也不用猜。

## 錯誤要分得出來

「日期時間格式錯」與「劇本名稱找不到」是兩件事，使用者要修的地方完全
不同。混成一句「格式錯誤」等於叫他自己猜是哪裡錯。
"""

from __future__ import annotations

import re
from datetime import date as Date, time as Time

KEYWORD = "預訂"

# 解析結果的第一個值。呼叫端照這個決定回哪一句。
UNKNOWN = "unknown"           # 哪一種指令都不是
BAD_DATETIME = "bad_datetime"  # 是預訂，但日期時間讀不出來
BOOKING = "booking"            # 讀得出來
DAY = "day"                    # 查某一天的場次
CALENDAR = "calendar"          # 查一個或好幾個月的行事曆
BAD_DATE = "bad_date"          # 是查詢的樣子，但沒有這一天／這個月（2026/2/30場次、2026/13行事曆）
BAD_RANGE = "bad_range"        # 行事曆的範圍倒過來，或超過 MAX_MONTHS 個月

# 一次最多幾個月。十二個月排成 3 × 4 已經要放大才看得清楚，再多只是更小。
MAX_MONTHS = 12

# 全形 → 半形。使用者在手機上很容易打到全形，那不是他的錯。
_NORMALISE = str.maketrans({
    "／": "/", "：": ":", "－": "-", "　": " ",
    "（": "(", "）": ")",
    # 範圍的波浪號。手機注音輸入打出來的多半是全形的那一個。
    "～": "~", "〜": "~",
})


def strip_mentions(text: str, mentionees: list[dict]) -> str:
    """把 @某某 那幾段從訊息裡挖掉，剩下的才是指令。

    ★ LINE 給的 index／length 是 **UTF-16 編碼單位**的偏移量，不是 Python
      的字元數。中文在 BMP 內兩者相同，但只要訊息裡有一個 emoji（代理對，
      佔 2 個 UTF-16 單位、1 個 Python 字元），直接拿去切字串就會偏移，
      而且只有在那種訊息上才會錯——最難重現的那一類 bug。

      所以先轉成 utf-16-le、照位元組切、再轉回來。
    """
    if not mentionees:
        return text.strip()
    buf = text.encode("utf-16-le")
    spans = sorted(
        ((int(m["index"]) * 2, (int(m["index"]) + int(m["length"])) * 2)
         for m in mentionees if "index" in m and "length" in m),
        reverse=True,
    )
    for start, end in spans:
        buf = buf[:start] + buf[end:]
    return buf.decode("utf-16-le", errors="replace").strip()


def _parse_date(token: str) -> Date | None:
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", token)
    if not m:
        return None
    try:
        return Date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        # 2026/2/30 這種：格式對、日子不存在。對使用者而言是同一件事。
        return None


def _parse_time(token: str) -> Time | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if not m:
        return None
    try:
        return Time(int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse(text: str) -> tuple[str, dict | None]:
    """回 (結果, 內容)。內容只有 BOOKING、DAY、CALENDAR 時才有東西。"""
    body = text.translate(_NORMALISE).strip()
    if body.startswith(KEYWORD):
        return _parse_booking(body[len(KEYWORD):])
    return _parse_query(body)


def _parse_booking(rest: str) -> tuple[str, dict | None]:
    rest = rest.strip()
    parts = rest.split()
    # 名稱 + 日期 + 時間，至少三段。少於三段代表日期時間沒打完。
    if len(parts) < 3:
        return BAD_DATETIME, None

    d = _parse_date(parts[-2])
    t = _parse_time(parts[-1])
    if d is None or t is None:
        return BAD_DATETIME, None

    # 中間全部當名稱。劇本名稱理論上不含空白，但使用者多打一個空白不該
    # 變成「找不到」——那個錯誤訊息會讓他去懷疑名稱本身。
    name = " ".join(parts[:-2]).strip()
    if not name:
        return BAD_DATETIME, None

    return BOOKING, {"name": name, "date": d, "time": t}


# 查詢的三種樣子。年份一樣必填（見開頭）。
_DAY = re.compile(r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*場次")
_CALENDAR = re.compile(r"(\d{4})(?:[/-](\d{1,2}))?\s*行事曆")
_RANGE = re.compile(r"(\d{4})[/-](\d{1,2})\s*~\s*(\d{4})[/-](\d{1,2})\s*行事曆")


def _parse_query(body: str) -> tuple[str, dict | None]:
    m = _DAY.fullmatch(body)
    if m:
        d = _parse_date(m.group(1))
        return (DAY, {"date": d}) if d else (BAD_DATE, None)

    m = _RANGE.fullmatch(body)
    if m:
        first = _month(m.group(1), m.group(2))
        last = _month(m.group(3), m.group(4))
    else:
        m = _CALENDAR.fullmatch(body)
        if not m:
            return UNKNOWN, None
        if m.group(2) is None:                    # 2026行事曆：整年
            first, last = _month(m.group(1), "1"), _month(m.group(1), "12")
        else:
            first = last = _month(m.group(1), m.group(2))

    if first is None or last is None:
        return BAD_DATE, None
    count = (last[0] - first[0]) * 12 + (last[1] - first[1]) + 1
    if not 1 <= count <= MAX_MONTHS:
        return BAD_RANGE, None
    months = [
        (first[0] + (first[1] - 1 + i) // 12, (first[1] - 1 + i) % 12 + 1)
        for i in range(count)
    ]
    return CALENDAR, {"months": months}


def _month(year: str, month: str) -> tuple[int, int] | None:
    y, m = int(year), int(month)
    # 0000 年沒有意義，Python 的 date 也不收。
    if not (1 <= y <= 9999 and 1 <= m <= 12):
        return None
    return y, m
