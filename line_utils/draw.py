"""LINE 出圖：把行事曆與某一天的場次畫成 PNG。

只畫、不查資料庫——要畫的東西由 images.py 備好傳進來。跟 handler.py 不碰
HTTP 同一個理由：這一層給一組假資料就測得起來、看得到圖。

## 照網站的 CSS 畫

尺寸、字級、顏色、點的規則都抄自 web 的 style.css（eli 分支）、
calendarpage.js 與 admin.js 的 storeCard。下面的數字都是 CSS px，最後乘上
scale——跟手機螢幕是 2～3 倍一樣的道理，放大看才不會糊。

★ 網站改樣式時這裡**不會**跟著變，兩邊各一份。這是用 Pillow 畫的代價：
  讓瀏覽器畫再截圖才會一模一樣，但無頭瀏覽器在 Render 免費方案的 512MB 上
  跑不動。改網站的日曆或場次卡片時，記得這裡也有一份。

★ 配色是 eli 的——目前只有她開 LINE。哪天第二家店開 LINE，配色要改成跟著
  店走，不然那家店的圖會是 eli 的顏色。

## 字型

Noto Sans TC，跟網站同一套（網站從 Google Fonts 載）。檔案在 fonts/，
OFL 授權；OFL 要求字型檔跟授權書一起散布，所以授權書放在同一個目錄。

★ 這是可變字型，**預設粗細是 100（Thin）不是 400**。每個字型物件都要明確
  設粗細，漏掉的話字會細得幾乎看不見，而且不會有任何錯誤。
"""

from __future__ import annotations

import io
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).resolve().parent / "fonts" / "NotoSansTC-Variable.ttf"

# ── 顏色：eli 的 style.css（:root）──────────────────────────────

BG = "#ffffff"          # --bg；卡片的 --surface 也是白，所以卡片只畫邊框
INK = "#141413"         # --ink
INK_SOFT = "#45443f"    # --ink-soft
INK_FAINT = "#6b6a66"   # --ink-faint
BORDER = "#e6e5e3"      # --border
BUSY_BG = "#efedea"     # --busy-bg
BUSY_RING = "#141413"   # --busy-ring
DOT = {
    "gm_confirm": "#c8890c",   # --dot-wait
    "gm_reviewed": "#cf5148",  # --dot-deposit
    "booked": "#c3aee0",       # --dot-booked：粉紫（案主 2026-09-19 指定）
    "ended": "#625d56",        # --dot-ended：深灰
}

# ── 版面（CSS px）────────────────────────────────────────────────

PAD_X = 18              # .section 左右留白（--pad）
PAD_Y = 20              # .section 上方留白；圖的下緣也留一樣多
CELL_W = 48             # 一格的寬。網站是 1fr，手機上 7 格加 6 道間隔約 348
CELL_H = 44             # .cal__day
GAP = 2                 # .cal__grid 的 gap
MONTH_W = CELL_W * 7 + GAP * 6
HEAD_H = 44             # 月份標題那一列：網站兩側 44px 的翻頁鈕撐出來的高度（圖上不畫按鈕）
HEAD_GAP = 16           # .cal__head 的 margin-bottom 8，加 .section 的 gap 8
SECTION_GAP = 8         # .section 的 gap：日曆與圖例之間、圖例與下一行之間
DOW_H = 12 * 1.5 + 4    # .cal__dow：12px、行高 1.5，加 padding-bottom 4
NUM_LH = 15 * 1.1       # .cal__day--mark 的行高 1.1
MARKS_GAP = 3           # 數字與點之間（.cal__day--mark 的 gap）
MARKS_H = 6             # .cal-marks
DOT_D = 6               # .cal-dot
DOT_GAP = 3             # .cal-marks 的 gap
LINE_W, LINE_H = 22, 3  # .cal-line
MAX_DOTS = 3            # 跟 calendarpage.js 同一個數字：四場以上改畫一條線
LEGEND_LH = 12 * 1.5    # .cal-legend：12px
STAMP_LH = 12 * 1.5     # 最下面「截至」那一行，同 .field__hint
BLOCK_GAP = 36          # 好幾個月排在一起時，月與月之間（比格子寬，才分得出是兩個月）
WEEKDAYS = "日一二三四五六"

# 形狀先放大四倍畫、再縮回來。Pillow 畫圓與圓角本身不反鋸齒，
# 直接畫的話點的邊緣是鋸齒狀的。
_SS = 4


def calendar_png(months: list[tuple[int, int]], days: dict[str, list[str]],
                 busy: set[str], legend: list[tuple[str, str]], *,
                 now: datetime) -> bytes:
    """一個或多個月的行事曆（店家的場次行事曆）。

    months  [(年, 月), ...]，依序排：一個月一欄、兩到六個月兩欄、再多三欄
            （十二個月就是 3 × 4）。欄數愈少、放到手機寬度時字愈大，所以能兩欄
            就兩欄；七個月以上兩欄會太長，整張縮進螢幕反而更小。
    days    {'2026-09-05': ['booked', 'ended']}：一場一個狀態、依開始時間排好
    busy    店家忙碌日的 ISO 字串
    legend  [(狀態, 中文)]：這家店到得了的狀態，順序就是圖例的順序
    """
    n = len(months)
    cols = 1 if n == 1 else 2 if n <= 6 else 3
    rows = -(-n // cols)
    fonts = _Fonts(3 if cols == 1 else 2)
    content_w = cols * MONTH_W + (cols - 1) * BLOCK_GAP

    # 一個月時照實際週數，跟網站一樣；好幾個月時每個月都留六週，並排的月份
    # 格子才對得齊。
    weeks = _weeks(*months[0]) if n == 1 else 6
    block_h = HEAD_H + HEAD_GAP + DOW_H + GAP + weeks * CELL_H + (weeks - 1) * GAP
    placed, legend_h = _legend_layout(fonts, content_w, legend)
    grid_h = rows * block_h + (rows - 1) * BLOCK_GAP

    cv = _Canvas(PAD_X * 2 + content_w,
                 PAD_Y + grid_h + SECTION_GAP + legend_h + SECTION_GAP + STAMP_LH + PAD_Y,
                 fonts)
    for i, (year, month) in enumerate(months):
        _month(cv, PAD_X + (i % cols) * (MONTH_W + BLOCK_GAP),
               PAD_Y + (i // cols) * (block_h + BLOCK_GAP), year, month, days, busy)
    y = PAD_Y + grid_h + SECTION_GAP
    _legend(cv, PAD_X, y, placed)
    _stamp(cv, PAD_X, y + legend_h + SECTION_GAP, now)
    return cv.png()


def day_png(day: date, items: list[dict], has_more: bool, *, now: datetime) -> bytes:
    """某一天的場次，一場一張卡片（網站 storeCard 的樣子）。已取消的也列——
    跟網站同一條規則：點不畫已取消，清單全部列出。

    ★ 卡片上只有劇本、日期時間、狀態，**不放預訂者與備註**（案主 2026-09-19）。
      這張圖會貼在有客人的群組裡，網站上那張卡片是給店家自己看的。
    """
    fonts = _Fonts(3)
    content_w = MONTH_W
    inner_w = content_w - 2 - 18 * 2          # .card：邊框各 1、左右 padding 18
    cards = [(_wrap(fonts, it["mmg_name"], inner_w, size=15, weight=700), it)
             for it in items]

    title_h = 15 * 1.5
    if cards:
        body_h = sum(_card_h(len(lines)) for lines, _ in cards) + 8 * (len(cards) - 1)
        if has_more:
            body_h += 8 + 12 * 1.5
    else:
        body_h = 40 + 14 * 1.5 + 40           # .empty：上下 padding 40、14px

    cv = _Canvas(PAD_X * 2 + content_w,
                 PAD_Y + title_h + SECTION_GAP + body_h + SECTION_GAP + STAMP_LH + PAD_Y,
                 fonts)
    weekday = WEEKDAYS[(day.weekday() + 1) % 7]
    cv.text(PAD_X, PAD_Y, f"{day.year} 年 {day.month} 月 {day.day} 日（{weekday}）",
            size=15, weight=700)
    y = PAD_Y + title_h + SECTION_GAP
    if not cards:
        cv.text(PAD_X + content_w / 2, y + 40, "這天沒有場次",
                size=14, fill=INK_FAINT, align="m")
    for lines, item in cards:
        h = _card_h(len(lines))
        _card(cv, PAD_X, y, content_w, h, lines, item)
        y += h + 8
    if cards and has_more:
        # 一天超過一頁（50 場）幾乎不會發生，但發生時要講出來，跟網站同一句。
        cv.text(PAD_X, y, "這天的場次太多，其餘請到場次管理查看", size=12, fill=INK_FAINT)
    _stamp(cv, PAD_X, PAD_Y + title_h + SECTION_GAP + body_h + SECTION_GAP, now)
    return cv.png()


# ── 一個月 ────────────────────────────────────────────────────────

def _weeks(year: int, month: int) -> int:
    return (_lead(year, month) + monthrange(year, month)[1] + 6) // 7


def _lead(year: int, month: int) -> int:
    """1 號前面要空幾格。週日開頭，跟網站一樣。"""
    return (date(year, month, 1).weekday() + 1) % 7


def _month(cv: _Canvas, x0: float, y0: float, year: int, month: int,
           days: dict[str, list[str]], busy: set[str]) -> None:
    cv.text(x0 + MONTH_W / 2, y0, f"{year} 年 {month} 月",
            size=15, weight=700, lh=HEAD_H, align="m")
    y = y0 + HEAD_H + HEAD_GAP
    for i, w in enumerate(WEEKDAYS):
        cv.text(x0 + i * (CELL_W + GAP) + CELL_W / 2, y, w,
                size=12, fill=INK_FAINT, align="m")
    y += DOW_H + GAP

    lead = _lead(year, month)
    for day in range(1, monthrange(year, month)[1] + 1):
        i = lead + day - 1
        cx = x0 + (i % 7) * (CELL_W + GAP)
        cy = y + (i // 7) * (CELL_H + GAP)
        iso = f"{year:04d}-{month:02d}-{day:02d}"
        if iso in busy:
            # 網站是灰底加 box-shadow inset 2px。inset 的框畫在 1px 透明邊框的
            # 內側，所以外框往內縮 1px、圓角從 6 變 5。
            cv.shape(cx, cy, CELL_W, CELL_H, 6, BUSY_BG)
            cv.shape(cx + 1, cy + 1, CELL_W - 2, CELL_H - 2, 5, BUSY_RING, ring=2)
        # 數字與點這一整塊在格子裡上下置中（flex 直排、gap 3）。沒有點的日子
        # 也留那一列的高度，數字才不會高高低低——網站 2026-09-18 修過同一件事。
        top = cy + (CELL_H - (NUM_LH + MARKS_GAP + MARKS_H)) / 2
        cv.text(cx + CELL_W / 2, top, str(day), size=15, lh=NUM_LH, align="m")
        _marks(cv, cx + CELL_W / 2, top + NUM_LH + MARKS_GAP, days.get(iso, []))


def _marks(cv: _Canvas, center_x: float, top: float, statuses: list[str]) -> None:
    """一場一個點、依開始時間由左到右；四場以上改畫一條線（案主定案）。"""
    if len(statuses) > MAX_DOTS:
        cv.shape(center_x - LINE_W / 2, top + (MARKS_H - LINE_H) / 2,
                 LINE_W, LINE_H, 2, INK)
        return
    x = center_x - (len(statuses) * (DOT_D + DOT_GAP) - DOT_GAP) / 2
    for status in statuses:
        # 不認得的狀態畫淡灰，不要整張圖因為一個點失敗。
        cv.shape(x, top, DOT_D, DOT_D, DOT_D / 2, DOT.get(status, INK_FAINT))
        x += DOT_D + DOT_GAP


def _legend_layout(fonts: _Fonts, width: float, legend: list[tuple[str, str]]):
    """.cal-legend：一個接一個往右排，放不下就換行（gap 6px 14px）。

    回傳 ([(x, 第幾列, 狀態, 中文)], 總高度)。先排好才知道整張圖要多高。
    """
    placed, x, row = [], 0.0, 0
    for key, label in legend:
        w = DOT_D + 6 + fonts(12).getlength(label) / fonts.scale
        if x > 0 and x + w > width:
            x, row = 0.0, row + 1
        placed.append((x, row, key, label))
        x += w + 14
    if not placed:
        return placed, 0
    return placed, 6 + (row + 1) * LEGEND_LH + row * 6


def _legend(cv: _Canvas, x0: float, y0: float, placed) -> None:
    for x, row, key, label in placed:
        y = y0 + 6 + row * (LEGEND_LH + 6)
        cv.shape(x0 + x, y + (LEGEND_LH - DOT_D) / 2, DOT_D, DOT_D, DOT_D / 2,
                 DOT.get(key, INK_FAINT))
        cv.text(x0 + x + DOT_D + 6, y, label, size=12, fill=INK_SOFT)


def _stamp(cv: _Canvas, x: float, y: float, now: datetime) -> None:
    """最下面一行「截至幾點」。

    ★ 網站上沒有這一行，是圖才需要：網站永遠是現在的資料，圖會留在聊天紀錄
      裡。一週後往上翻看到的月曆，要看得出那是什麼時候的樣子。
    """
    cv.text(x, y, f"截至 {now:%Y/%-m/%-d %H:%M}", size=12, fill=INK_FAINT)


# ── 一張場次卡片 ──────────────────────────────────────────────────

def _card_h(title_lines: int) -> float:
    # 邊框 1 + padding 14 + 標題 + gap 4 + 日期時間 + gap 4 + 狀態 + padding 14 + 邊框 1
    return 1 + 14 + title_lines * 15 * 1.5 + 4 + 13 * 1.5 + 4 + 13 * 1.5 + 14 + 1


def _card(cv: _Canvas, x: float, y: float, w: float, h: float,
          title_lines: list[str], item: dict) -> None:
    cv.shape(x, y, w, h, 10, BORDER, ring=1)        # .card：1px 邊框、圓角 10
    tx, ty = x + 1 + 18, y + 1 + 14
    for line in title_lines:                          # .list-item__title
        cv.text(tx, ty, line, size=15, weight=700)
        ty += 15 * 1.5
    ty += 4
    # 網站這一行是「日期 時間 · 預訂者」，預訂者拿掉了（見 day_png）。
    cv.text(tx, ty, f"{item['session_date']} {item['session_time']}",
            size=13, fill=INK_SOFT)                   # .list-item__meta
    ty += 13 * 1.5 + 4
    cv.text(tx, ty, item.get("status_label") or item["status"], size=13)  # .list-item__status


def _wrap(fonts: _Fonts, text: str, width: float, *, size: float, weight: int = 400) -> list[str]:
    """照寬度折行、逐字斷——中文本來就是任兩字之間都能斷。英文長字會被切開，
    劇本名稱很少那樣，不值得為它寫斷字。"""
    font = fonts(size, weight)
    limit = width * fonts.scale
    lines, line = [], ""
    for ch in text:
        if line and font.getlength(line + ch) > limit:
            lines.append(line)
            line = ch
        else:
            line += ch
    lines.append(line)
    return lines


# ── 畫布 ─────────────────────────────────────────────────────────

class _Fonts:
    """這一張圖用到的字型。每張圖各開一組，不跨請求共用。

    ★ 共用的話要加鎖：FreeType 的字型物件畫字時會改自己的狀態，兩個執行緒
      同時拿同一個去畫會互相踩。開一個只要一兩毫秒，不值得為它引進共用狀態。
    """

    def __init__(self, scale: int):
        self.scale = scale
        self._made: dict[tuple[float, int], ImageFont.FreeTypeFont] = {}

    def __call__(self, size: float, weight: int = 400) -> ImageFont.FreeTypeFont:
        font = self._made.get((size, weight))
        if font is None:
            font = ImageFont.truetype(str(FONT_PATH), round(size * self.scale))
            font.set_variation_by_axes([weight])     # 見開頭：預設是 Thin
            self._made[(size, weight)] = font
        return font


class _Canvas:
    """座標一律用 CSS px 給，這裡負責乘上 scale。"""

    def __init__(self, width: float, height: float, fonts: _Fonts):
        self.fonts = fonts
        self.s = fonts.scale
        self.img = Image.new("RGB", (round(width * self.s), round(height * self.s)), BG)
        self.draw = ImageDraw.Draw(self.img)
        self._masks: dict[tuple, Image.Image] = {}

    def text(self, x: float, top: float, s: str, *, size: float, weight: int = 400,
             fill: str = INK, lh: float | None = None, align: str = "l") -> None:
        """畫一行字，位置照瀏覽器排版：top 是行框頂端，行框高 lh（預設字級 × 1.5），
        字型的上下緣（ascent + descent）在行框裡置中，字落在基線上。

        align 是水平方向的錨點：l 從 x 往右、m 以 x 為中心。
        """
        font = self.fonts(size, weight)
        ascent, descent = font.getmetrics()
        box = (size * 1.5 if lh is None else lh) * self.s
        baseline = top * self.s + (box - (ascent + descent)) / 2 + ascent
        self.draw.text((x * self.s, baseline), s, font=font, fill=fill, anchor=align + "s")

    def shape(self, x: float, y: float, w: float, h: float, radius: float,
              fill: str, *, ring: float = 0) -> None:
        """反鋸齒的圓角矩形；圓點就是 w = h = 2 × radius。ring > 0 時只畫外圈
        那一道框，寬 ring。"""
        size = (round(w * self.s), round(h * self.s))
        key = (size, radius, ring)
        mask = self._masks.get(key)
        if mask is None:
            mask = self._masks[key] = _mask(size, radius * self.s, ring * self.s)
        self.img.paste(fill, (round(x * self.s), round(y * self.s)), mask)

    def png(self) -> bytes:
        """先轉成 256 色再存。

        ★ 存檔是整張圖最花時間的一步（十二個月那張，本機上畫 50ms、存 450ms），
          而 Render 免費方案只有 0.1 顆 CPU。轉成 256 色之後存檔快一倍、檔案
          小三倍（193KB → 59KB）。代價是顏色偏一點點：實測設計用的那幾個顏色
          每個色版最多差 3（255 階裡的 3），點的粉紫與深灰差 0～1，肉眼看不出來。
        """
        buf = io.BytesIO()
        self.img.quantize(256, method=Image.Quantize.FASTOCTREE).save(buf, "PNG")
        return buf.getvalue()


def _mask(size: tuple[int, int], radius: float, ring: float) -> Image.Image:
    w, h = size[0] * _SS, size[1] * _SS
    # 跟 CSS 一樣：圓角大過短邊的一半，就被壓到一半（.cal-line 高 3、圓角 2）。
    r = min(radius, size[0] / 2, size[1] / 2) * _SS
    big = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=round(r), fill=255)
    if ring:
        k = round(ring * _SS)
        d.rounded_rectangle([k, k, w - 1 - k, h - 1 - k], radius=round(max(0.0, r - k)), fill=0)
    return big.reduce(_SS)
