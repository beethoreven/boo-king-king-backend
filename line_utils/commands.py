"""群組訊息 → 指令。

## 指令只有一個

    @bot 預訂 丹水山莊 2026/10/5 14:00

「預訂」以外一律回「我不知道你在說什麼」。不做模糊比對、不猜意圖——
這支 bot 會真的寫入店家的營運資料，猜錯的代價是一筆不存在的預約，而
猜對省下的只是使用者少打兩個字。

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
UNKNOWN = "unknown"           # 不是「預訂」開頭
BAD_DATETIME = "bad_datetime"  # 是預訂，但日期時間讀不出來
BOOKING = "booking"            # 讀得出來

# 全形 → 半形。使用者在手機上很容易打到全形，那不是他的錯。
_NORMALISE = str.maketrans({
    "／": "/", "：": ":", "－": "-", "　": " ",
    "（": "(", "）": ")",
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
    """回 (結果, 內容)。內容只有 BOOKING 時才有東西。"""
    body = text.translate(_NORMALISE).strip()
    if not body.startswith(KEYWORD):
        return UNKNOWN, None

    rest = body[len(KEYWORD):].strip()
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
