"""LINE bot：群組裡 tag 一下就把場次訂進系統。

## 整包在什麼條件下存在

兩個環境變數缺任何一個，整支停用——路由回 503、不收訊、不推播。
理由跟 BREVO_API_KEY 一樣：憑證不是行為，不放進 functions 表。
步經徑的部署不設，等於它完全沒有 LINE 這個面——資料庫裡也不建 LINE 的
表與欄位（見 db/schema.py 的 ensure_schema）。

    LINE_CHANNEL_SECRET        驗 webhook 簽章用
    LINE_CHANNEL_ACCESS_TOKEN  呼叫 Reply／Push API 用

## 兩種訊息，成本完全不同

    Reply   回應群組裡的那一則。**不計入免費額度**，所以訂場流程零成本
    Push    主動私訊店主。**吃每月 200 則**，由 roles.line_notification 控制

## 為什麼是 5 秒

LINE 的 webhook 逾時是 5 秒，超過當失敗。所以這一條路徑上不做任何慢
的事：Reply 是同步的（token 有時效，非做不可），Push 丟到背景佇列。
真正的風險不在程式，在 Render 免費方案的冷啟動（約 60 秒）——那靠
24/7 保活解決，不是靠這裡的程式。
"""
