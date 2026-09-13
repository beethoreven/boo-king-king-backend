"""Webhook 簽章驗證。**這是把關，不是裝飾。**

那支路由沒有登入、沒有 session、網址是公開的。唯一能證明「這包資料真的
來自 LINE」的就是簽章——驗不過就一定要拒絕，否則任何人都能 POST 一包
假事件進來，在店家的系統上訂出場次。

## 算法

    base64( HMAC-SHA256( channel_secret, 原始 request body ) )

★ **一定要用原始 bytes**，不能先 json.loads 再 dumps 回去。序列化會改變
  空白與鍵的順序，算出來的雜湊就對不上——症狀是「明明設定都對卻一直
  401」，而且完全看不出原因。Flask 要用 request.get_data()。
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    """算出這包 body 的簽章。驗證與測試共用同一支，不要各寫一份。"""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def verify(body: bytes, header: str | None, secret: str) -> bool:
    """header 是 X-Line-Signature。缺、空、或對不上一律 False。

    ★ 用 hmac.compare_digest 而不是 ==：字串比較會在第一個不同的位元組
      就回傳，回應時間因此洩漏「對了幾個字元」，理論上可以逐位元組猜出
      正確簽章。定時比較沒有這個性質。
    """
    if not header or not secret:
        return False
    return hmac.compare_digest(sign(body, secret), header)
