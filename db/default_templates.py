"""五封通知信的預設內容。

只在 `notification_template` 還沒有那一列時寫進去（ON CONFLICT DO NOTHING），
所以案主用 SQL 改過的內容不會被下一次部署蓋掉。要恢復預設就把那一列刪掉，
下次啟動會重新塞回來。

## 版型上的決定（2026-09-01 定案，逐項都有理由）

★ **沒有圖片、沒有 logo。** 圖片要有地方放，而通知信裡的外部圖片會被多數
  信箱預設擋掉——收件人看到的是破圖框。圖片多的信也更容易被判成廣告。

★ **資料用左側色條的區塊，不用表格。** 表格在手機信箱 app 上很容易被壓縮
  到亂掉；這個區塊在任何寬度下都只是往下堆。

★ **粗體只給三種東西**：劇本名、場次時間，以及那句「會怎樣」（不會成立／
  已經成立／被停權）。全部都粗等於都不粗。

★ **按鈕是 <a> 加行內樣式，不是圖片按鈕。** 在不支援樣式的信箱裡它會退化成
  一個普通連結，仍然點得到；圖片按鈕在那種情況下會整個消失。

★ **字型用系統字型堆疊。** 信箱不會去載 Google Fonts，指定了也是白指定。

★ **固定淺色。** HTML 信的深色模式在各家客戶端行為差異很大，做了反而可能
  變成深色字配深色底。

★ 樣式**全部寫成行內 style**。信箱會把 <style> 區塊丟掉，寫在那裡等於沒寫。
"""

from __future__ import annotations

# 共用的片段。用字串組裝而不是做成模板繼承——這裡只有五封信，多一層抽象
# 換到的東西比它自己的重量少。
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans TC',"
    "'Microsoft JhengHei',sans-serif"
)
_WRAP = (
    f"font-family:{_FONT};max-width:600px;margin:0 auto;"
    "padding:26px 24px 28px;color:#1a1a1a;font-size:15px;"
    "line-height:1.8;background:#ffffff;"
)
_BOX = (
    "background:#faf7f1;border-left:3px solid #c8975a;padding:14px 18px;"
    "margin:0 0 20px;font-size:14.5px;line-height:1.9;"
)
_BOX_ALERT = (
    "background:#fdf2ee;border-left:3px solid #c0563c;padding:14px 18px;"
    "margin:0 0 20px;font-size:14.5px;line-height:1.9;"
)
_KEY = "display:inline-block;min-width:4.5em;color:#7a7166;"
_BTN = (
    "display:inline-block;background:#c8975a;color:#241d12;text-decoration:none;"
    "padding:11px 24px;border-radius:6px;font-weight:700;font-size:15px;"
)
_FOOT = (
    "margin:28px 0 0;padding-top:16px;border-top:1px solid #eae4da;"
    "color:#8a8177;font-size:13px;"
)
_HINT = "margin:0 0 24px;color:#5a5248;font-size:14px;"


def _row(key: str, value: str) -> str:
    return f'<div><span style="{_KEY}">{key}</span>{value}</div>'


def _btn(text: str) -> str:
    return f'<a href="{{{{site_url}}}}" style="{_BTN}">{text}</a>'


def _foot(extra: str = "") -> str:
    tail = f"<br>{extra}" if extra else ""
    return f'<p style="{_FOOT}">{{{{site_name}}}}{tail}</p>'


# key -> (subject, body_html)
TEMPLATES: dict[str, tuple[str, str]] = {
    # ── ① 主持人：玩家指定了他 ──────────────────────────────
    "host_assigned": (
        "【指定通知】你被指定主持《{{mmg_name}}》{{session_short}}",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 14px;">{{{{host_name}}}} 您好：</p>
<p style="margin:0 0 20px;">有一筆新的預約指定您擔任主持人，請前往確認。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("角色", "{{gm_role}}")}
{_row("預定者", "{{player_name}}")}
</div>
<p style="margin:0 0 24px;">在您確認之前，這筆預約<strong>不會成立</strong>。</p>
{_btn("前往確認")}
{_foot()}
</div>''',
    ),
    # ── ② 管理員：主持人都確認完了 ──────────────────────────
    "admin_reviewed": (
        "【場次確認通知】《{{mmg_name}}》{{session_short}} 主持人已全數確認，待收訂金",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;">有一筆預約的主持人已<strong>全部確認</strong>，請通知預定者支付訂金。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("主持", "{{gm_list}}")}
{_row("預定者", "{{player_name}}")}
{_row("訂金", "<strong>{{deposit}}</strong>")}
</div>
{_btn("前往處理")}
{_foot()}
</div>''',
    ),
    # ── ③ 玩家：預約成立 ────────────────────────────────────
    "player_booked": (
        "【預約成功】《{{mmg_name}}》{{session_short}}",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 14px;">{{{{player_name}}}} 您好：</p>
<p style="margin:0 0 20px;">您的預約<strong>已經成立</strong>，我們到時候見。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("主持", "{{gm_list}}")}
</div>
{_btn("查看我的預約")}
{_foot("（請勿回覆）")}
</div>''',
    ),
    # ── ④ 管理員：有人被記點 ────────────────────────────────
    #
    # ★ 跨過停權門檻的那一次**不會**發這封，只發 ⑤——否則同一個事件會
    #   同時寄兩封講同一件事的信。見 notifications.on_abuse_point。
    "abuse_point": (
        "【異常行為通知】{{user_name}} 累計 {{total}} 點",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;">有使用者觸發了異常行為紀錄。</p>
<div style="{_BOX}">
{_row("使用者", "<strong>{{user_name}}</strong>（{{user_email}}）")}
{_row("類型", "{{kind}}")}
{_row("本次", "+{{added}} 點")}
{_row("累計", "<strong>{{total}} 點</strong>")}
{_row("時間", "{{at}}")}
</div>
<p style="{_HINT}">累計達到門檻會自動停權。目前設定：{{{{rules}}}}。</p>
{_btn("查看異常紀錄")}
{_foot()}
</div>''',
    ),
    # ── ⑤ 管理員：有人被停權 ────────────────────────────────
    "abuse_banned": (
        "【停權通知】{{user_name}} 已停權{{until_short}}",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;">有使用者因異常行為<strong>被系統自動停權</strong>。</p>
<div style="{_BOX_ALERT}">
{_row("使用者", "<strong>{{user_name}}</strong>（{{user_email}}）")}
{_row("累計", "{{total}} 點")}
{_row("停權至", "<strong>{{until}}</strong>")}
{_row("原因", "{{kind}}")}
</div>
<p style="{_HINT}">若為誤判，到「使用者管理」把狀態改回<strong>啟用</strong>即可解除，異常點數會一併歸零。</p>
{_btn("前往處理")}
{_foot()}
</div>''',
    ),
}


def ensure(cur) -> None:
    """把還不存在的範本寫進去。已經存在的**不動**——那可能是案主改過的。"""
    for key, (subject, body) in TEMPLATES.items():
        cur.execute(
            "INSERT INTO notification_template (key, subject, body_html) "
            "VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
            (key, subject, body),
        )
