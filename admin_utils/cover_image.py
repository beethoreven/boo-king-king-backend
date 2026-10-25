"""簡介頁的封面圖：收到上傳的檔案 → 驗證 → 縮小 → 轉成 WebP。

## 為什麼一律重新編碼，不直接存原檔

  1. **擋掉偽裝成圖片的檔案。** 只看副檔名或 Content-Type 擋不住「前面是圖片、
     後面藏別的東西」的檔案。重新編碼之後存下來的只有像素，原檔的其他內容
     全部丟掉。
  2. **清掉照片的中繼資料。** 手機拍的照片帶著 EXIF，裡面常有 GPS 座標——
     店家拍店裡的劇本盒，就等於把店的位置（或家裡的位置）公開出去。
     重新編碼時不帶任何 EXIF。
  3. **大小可控。** 原檔可能是 8MB 的相機原圖，每個看簡介的人都要下載一次。
     縮到最長邊 1200px、轉成 WebP 之後通常一兩百 KB。

## 尺寸：不裁切

只限制最大寬度與最大高度，等比例縮進去。直的、橫的、方的都照原比例，
不會被切掉任何一部分（裁哪裡應該是店家自己決定的事，不該由程式猜）。

## 擋住「解壓縮炸彈」

一張幾 KB 的 PNG 可以宣告自己是 50000×50000，解開來要好幾 GB 記憶體——
Render 免費方案只有 512MB，一張就能讓服務重開。所以先只讀表頭的尺寸，
超過上限直接拒絕，根本不解碼像素。
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

# 上傳檔案的大小上限（位元組）。app.py 的上傳路由用同一個數字放寬請求大小。
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# 縮圖之後的最大寬、高（等比例縮進這個框，不裁切）。
MAX_WIDTH = 1200
MAX_HEIGHT = 1600

# 原圖最多幾個像素才肯解開。4000 萬 ≈ 8000×5000，比任何手機或相機的原圖都大，
# 但遠小於「解開來會撐爆記憶體」的尺寸（見檔案開頭）。
MAX_SOURCE_PIXELS = 40_000_000

# 收哪幾種格式。手機的 HEIC 不在裡面：Pillow 沒有外掛讀不了，而 iPhone 從
# 網頁上傳照片時本來就會轉成 JPEG。
ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

WEBP_QUALITY = 82


class CoverError(ValueError):
    """上傳的檔案不能用。訊息直接給店家看。"""


def to_webp(raw: bytes) -> bytes:
    """驗證並轉檔。不合格就拋 CoverError。"""
    if not raw:
        raise CoverError("沒有收到圖片")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise CoverError(f"圖片太大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")

    with warnings.catch_warnings():
        # Pillow 對「大但還沒到兩倍上限」的圖只發警告不拋例外，這裡一律當錯誤。
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            img = Image.open(io.BytesIO(raw))
            fmt = img.format
            width, height = img.size          # 只讀表頭，還沒解開像素
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            # 尺寸大到 Pillow 在讀表頭時就自己拒絕（超過它內建上限的兩倍）。
            raise CoverError("圖片的像素太多，請先縮小再上傳") from None
        except Exception:  # noqa: BLE001 - 讀不出來就是不是圖片，原因不重要
            raise CoverError("這不是圖片檔，或格式不支援（可用 JPG、PNG、WebP、GIF）") from None

        if fmt not in ACCEPTED_FORMATS:
            raise CoverError("格式不支援（可用 JPG、PNG、WebP、GIF）")
        if width * height > MAX_SOURCE_PIXELS:
            raise CoverError("圖片的像素太多，請先縮小再上傳")

        # 色彩描述檔（ICC）先記下來，下面轉檔時帶回去：iPhone 的照片是廣色域，
        # 少了它顏色會變淡。它只描述顏色，不含任何隱私資訊。
        icc_profile = img.info.get("icc_profile")
        try:
            img.load()
            # 手機照片常常是「存成橫的、靠 EXIF 說要轉 90 度」。照著轉正再丟掉
            # EXIF，不然丟掉 EXIF 之後圖會變成躺著的。
            img = ImageOps.exif_transpose(img)
        except Exception:  # noqa: BLE001 - 檔案壞掉、或解開時才發現是炸彈
            raise CoverError("圖片檔損壞，讀不出來") from None

    # 有透明度的保留透明度（WebP 支援），其餘一律轉成 RGB。調色盤模式（GIF、
    # 部分 PNG）直接存 WebP 會走樣，先轉。
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    img.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    # ★ exif 明確給空的：不同版本的 Pillow 對「沒給 exif 時要不要沿用原圖的」
    #   行為不一樣，不能靠預設值。
    save_args = {"format": "WEBP", "quality": WEBP_QUALITY, "method": 4, "exif": b""}
    if icc_profile:
        save_args["icc_profile"] = icc_profile
    img.save(out, **save_args)
    return out.getvalue()
