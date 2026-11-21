"""上傳圖（簡介頁的封面、角色圖）的把關：只有 JPEG、PNG、WebP、GIF 的解析器會被叫到。

為什麼要擋在「解析器被叫到」之前，見 admin_utils/cover_image.py 開頭。這裡不需要
資料庫，直接呼叫 to_webp()。
"""

from __future__ import annotations

import io
import struct
import time
import zlib

import pytest
from PIL import (BmpImagePlugin, EpsImagePlugin, IcoImagePlugin, Image, PcxImagePlugin,
                 PpmImagePlugin, TgaImagePlugin, TiffImagePlugin, XbmImagePlugin)

from admin_utils import cover_image


def _saved(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _png_header_only(width: int, height: int) -> bytes:
    """只有表頭的 PNG：宣告一個尺寸，後面幾乎沒有像素資料。讀表頭的程式只看得到尺寸。"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))


# 不收的格式：(名稱, 解析器的類別, 產生檔案的方式)。都是 Pillow 認得、而且以前會
# 先被解析一次才被「格式不支援」擋下來的。
_EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\n%%EndComments\nshowpage\n"
REFUSED = [
    ("XBM", XbmImagePlugin.XbmImageFile, lambda: _saved(Image.new("1", (16, 16)), "XBM")),
    ("TIFF", TiffImagePlugin.TiffImageFile, lambda: _saved(Image.new("RGB", (16, 16)), "TIFF")),
    ("BMP", BmpImagePlugin.BmpImageFile, lambda: _saved(Image.new("RGB", (16, 16)), "BMP")),
    ("ICO", IcoImagePlugin.IcoImageFile, lambda: _saved(Image.new("RGBA", (16, 16)), "ICO")),
    ("PPM", PpmImagePlugin.PpmImageFile, lambda: _saved(Image.new("RGB", (16, 16)), "PPM")),
    ("PCX", PcxImagePlugin.PcxImageFile, lambda: _saved(Image.new("RGB", (16, 16)), "PCX")),
    ("TGA", TgaImagePlugin.TgaImageFile, lambda: _saved(Image.new("RGB", (16, 16)), "TGA")),
    ("EPS", EpsImagePlugin.EpsImageFile, lambda: _EPS),
]


@pytest.mark.parametrize("name, parser, make", REFUSED, ids=[r[0] for r in REFUSED])
def test_other_formats_are_refused_before_their_parser_runs(name, parser, make, monkeypatch):
    calls = []
    original = parser._open

    def spy(self):
        calls.append(name)
        return original(self)

    monkeypatch.setattr(parser, "_open", spy)
    data = make()
    with pytest.raises(cover_image.CoverError, match="格式不支援"):
        cover_image.to_webp(data)
    assert calls == [], f"{name} 的解析器不該被叫到——它的弱點在格式檢查之前就打得到"


@pytest.mark.parametrize("fmt, mode", [("JPEG", "RGB"), ("PNG", "RGBA"), ("WEBP", "RGB"), ("GIF", "P")])
def test_the_four_accepted_formats_still_convert(fmt, mode):
    out = cover_image.to_webp(_saved(Image.new(mode, (2000, 1000)), fmt))
    img = Image.open(io.BytesIO(out))
    assert img.format == "WEBP"
    assert img.size == (1200, 600), "等比例縮進 1200×1600"


def test_multi_picture_jpeg_is_still_refused():
    """JPEG 的解析器遇到 MPO（一個檔案裝好幾張圖）會回報成 MPO，formats 擋不到，
    要靠後面那一道格式檢查。"""
    first, second = Image.new("RGB", (32, 32), "red"), Image.new("RGB", (32, 32), "blue")
    data = _saved(first, "MPO", save_all=True, append_images=[second])
    with pytest.raises(cover_image.CoverError, match="格式不支援"):
        cover_image.to_webp(data)


@pytest.mark.parametrize("width, height", [
    (8000, 6000),     # 4800 萬：超過我們自己的上限（4000 萬）
    (10000, 9000),    # 9000 萬：Pillow 會先發警告，這裡當成錯誤
    (50000, 50000),   # 25 億：Pillow 讀表頭時就自己拒絕
])
def test_huge_declared_sizes_are_refused_without_decoding(width, height):
    with pytest.raises(cover_image.CoverError, match="像素太多"):
        cover_image.to_webp(_png_header_only(width, height))


def test_a_gif_with_a_huge_comment_does_not_stall():
    """Pillow 13.0.0 之前，GIF 註解是一段一段接起來的，花的時間跟檔案大小的平方成正比：
    4 MB 的註解在開發機上要 5 秒多，Render 免費方案的 CPU 只有十分之一。
    升級之後同一個檔案約 0.06 秒。上限給得很寬，只防有人把 Pillow 降回舊版。"""
    data = _saved(Image.new("P", (4, 4)), "GIF", comment=b"x" * 4_000_000)
    started = time.perf_counter()
    cover_image.to_webp(data)
    assert time.perf_counter() - started < 2
