"""通知信的預設內容。

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

# 按鈕底下那行紅字。用紅色是因為它警告的是一個**不可逆而且沉默**的後果：
# 點了 Brevo 的「取消訂閱」之後，那個人會被加進封鎖名單，之後所有通知都
# 不會再寄給他——沒有錯誤、沒有退信，寄件端只會看到「送出成功」。
#
# ★ 那顆取消訂閱是 Brevo 自己加的，不是這裡產生的，所以擋不掉，只能提醒。
_WARN = "margin:14px 0 0;color:#c0392b;font-size:13.5px;font-weight:700;"
_WARN_TEXT = "請勿取消訂閱此信，否則將會丟失未來所有通知。"


def _row(key: str, value: str) -> str:
    return f'<div><span style="{_KEY}">{key}</span>{value}</div>'


# 懸掛縮排：值太長折到第二行時，第二行對齊值那一欄，不縮回標籤底下。
# 手機上最明顯——場次時間、聯絡方式都常常一行放不下。
_ROW_HANG = "padding-left:4.5em;text-indent:-4.5em;"


def _hrow(key: str, value: str, *, only_if: str | None = None) -> str:
    """一列資料（懸掛縮排版）。

    only_if 給一個變數名：寄出前那個變數沒有值（None 或空字串），整列就
    拿掉，見 notifications.render。

    ★ text-indent 會被 inline-block 的標籤繼承，所以標籤要自己歸零，不然
      標籤的字會被推出框外。
    """
    cond = f' data-if="{only_if}"' if only_if else ""
    return (
        f'<div{cond} style="{_ROW_HANG}">'
        f'<span style="{_KEY}text-indent:0;">{key}</span>{value}</div>'
    )


def _btn(text: str) -> str:
    """按鈕，底下固定跟著那行紅色警語。

    綁在一起而不是各封自己加，是為了讓「之後再新增一封信」不會漏掉——
    漏掉的後果是那封信的收件人少一道提醒，而這種漏掉沒有人會發現。

    ★ href 用 {{link}} 而不是 {{site_url}}：site_url 是站台根目錄，而根
      目錄一律開在預約畫面——主持人收到「你被指定了」點進來會看到預約
      畫面，還要自己再點一次「主持人介面」。link 由 notifications 依信別
      算出，指向該去的畫面與頁籤；對不到時它就等於 site_url。
    """
    return (
        f'<a href="{{{{link}}}}" style="{_BTN}">{text}</a>'
        f'<p style="{_WARN}">{_WARN_TEXT}</p>'
    )


def _foot() -> str:
    return f'<p style="{_FOOT}">{{{{site_name}}}}</p>'


# key -> (subject, body_html)
TEMPLATES: dict[str, tuple[str, str]] = {
    # ── ① 主持人：玩家指定了他 ──────────────────────────────
    "gm_assigned": (
        "【指定通知】你被指定主持《{{mmg_name}}》{{session_short}}",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 14px;">{{{{gm_name}}}} 您好：</p>
<p style="margin:0 0 20px;">有一筆新的預約指定您擔任主持人，請前往確認。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("角色", "{{gm_role}}")}
{_row("預訂者", "{{player_name}}")}
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
<p style="margin:0 0 20px;">有一筆預約的主持人已<strong>全部確認</strong>，請通知預訂者支付訂金。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("主持", "{{gm_list}}")}
{_row("預訂者", "{{player_name}}")}
{_row("訂金", "<strong>{{deposit}}</strong>")}
</div>
{_btn("前往處理")}
{_foot()}
</div>''',
    ),
    # ── ③ 玩家：預約成立 ────────────────────────────────────
    "player_booked": (
        "【預約成功】《{{mmg_name}}》{{session_short}}（請勿回覆）",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 14px;">{{{{player_name}}}} 您好：</p>
<p style="margin:0 0 20px;">您的預約<strong>已經成立</strong>，我們到時候見。</p>
<div style="{_BOX}">
{_row("劇本", "<strong>{{mmg_name}}</strong>")}
{_row("場次", "<strong>{{session}}</strong>")}
{_row("主持", "{{gm_list}}")}
</div>
{_btn("查看我的預約")}
{_foot()}
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
    # ── ⑥ 管理員：預約成立 ──────────────────────────────────
    #
    # 跟 ③（寄給玩家）同一個時間點，寄給店家這一側（案主 2026-09-14）。
    #
    # 選填的四列跟著功能表與資料出現，值是空字串就整列拿掉：
    #   主持  gm_mode 是 single 時不給——那種店的主持人就是店主本人
    #   聯絡  預訂者沒填 LINE ID 也沒填電話時不給
    #   訂金  deposit 關掉時不給
    #   備註  沒寫時不給；有換行就照原樣換行
    "admin_booked": (
        "【場次成立通知】《{{mmg_name}}》{{session_short}}",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;">有一筆預約<strong>已經成立</strong>。</p>
<div style="{_BOX}">
{_hrow("劇本", "<strong>{{mmg_name}}</strong>")}
{_hrow("場次", "<strong>{{session}}</strong>")}
{_hrow("主持", "{{gm_list}}", only_if="gm_list")}
{_hrow("預訂者", "{{player_name}}")}
{_hrow("聯絡", "{{contact}}", only_if="contact")}
{_hrow("訂金", "{{deposit_paid}}", only_if="deposit_paid")}
{_hrow("備註", "{{note}}", only_if="note")}
</div>
{_btn("查看場次")}
{_foot()}
</div>''',
    ),
    # ── ⑦ 管理員：忙碌日那天已經有場次 ──────────────────────
    #
    # 情境：管理員把某一天設成忙碌日，而那天本來就有非取消的場次。畫面上
    # 已經警告過他一次，這封信是要讓**其他**管理員知道——店裡有別人時，
    # 那些場次要怎麼處置需要有人接手。
    #
    # ★ 已結束的場次也算既存場次（案主 2026-09-18）：有人跑去把一個已經
    #   結束的日期標成忙碌，本身就是值得被看見的操作。
    "busy_day_set": (
        "【忙碌日提醒】{{date_short}} 已設為忙碌日，當天仍有場次",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;"><strong>{{{{admin_name}}}}</strong>已經將 <strong>{{{{date}}}}</strong> 設定為忙碌日，但當天有既存場次。</p>
<div style="{_BOX_ALERT}">
{_hrow("日期", "<strong>{{date}}</strong>")}
{_hrow("當天場次", "{{sessions}}")}
</div>
<p style="{_HINT}">建議先確認當天場次將如何處置，確保玩家已清楚處理辦法，將該場次改期或移至已取消，確保忙碌日沒有既存場次需處理。</p>
{_btn("查看場次行事曆")}
{_foot()}
</div>''',
    ),
    # ── ⑧ 管理員：忙碌日被排進場次 ──────────────────────────
    #
    # 情境：那天是忙碌日，而管理員照樣在當天新增場次，或把既有場次改到
    # 那一天（案主 2026-09-18：改期也算）。他自己按過對話框了，這封信同樣
    # 是寄給店裡其他管理員看的。
    "busy_day_booking": (
        "【忙碌日提醒】忙碌日 {{date_short}} 被排入場次",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 20px;"><strong>{{{{date}}}}</strong>為忙碌日，而<strong>{{{{admin_name}}}}</strong>在當天新增了場次。</p>
<div style="{_BOX_ALERT}">
{_hrow("劇本", "<strong>{{mmg_name}}</strong>")}
{_hrow("場次", "<strong>{{session}}</strong>")}
{_hrow("預訂者", "{{player_name}}")}
</div>
<p style="{_HINT}">建議先確認當天真的能開場，且店家有人力可配合。</p>
{_btn("查看場次行事曆")}
{_foot()}
</div>''',
    ),
    # ── ⑨ 主持人：忙碌日被排進場次而且指定了你 ──────────────
    #
    # 情境同⑧，但這一場指定了主持人。他要知道的不只是「有一場」，而是
    # 「這是管理員才做得到的操作，先確認再按確認」。
    #
    # ★ single 模式不寄（案主 2026-09-18：eli 那邊主持人相關全拔掉了）。
    #   那種店的主持人就是店主本人，她已經收到⑧了。
    "busy_day_gm": (
        "【忙碌日提醒】{{date_short}} 是忙碌日，但你被指定了場次",
        f'''<div style="{_WRAP}">
<p style="margin:0 0 14px;">{{{{gm_name}}}} 您好：</p>
<p style="margin:0 0 20px;"><strong>{{{{date}}}}</strong>為忙碌日，而<strong>{{{{admin_name}}}}</strong>在當天新增了場次並且指定了你。</p>
<div style="{_BOX_ALERT}">
{_hrow("劇本", "<strong>{{mmg_name}}</strong>")}
{_hrow("場次", "<strong>{{session}}</strong>")}
</div>
<p style="{_HINT}">這是只有管理員有權限的操作，建議和管理員確認此操作是否正確，當天確實有開場計劃，以及你本身的帶場意願。確認完後，再行上系統確認指定。</p>
{_btn("查看指定行事曆")}
{_foot()}
</div>''',
    ),
}


# 改過名字的範本 key：{舊: 新}。
#
# ★ **key 是資料庫的主鍵，改名一定要帶遷移。** 少了這一步的話：舊那列
#   （host_assigned）沒有人再讀它，ensure() 會用預設內容插一列新的
#   （gm_assigned）——信照寄，但**那一列的 enabled 開關會回到預設值**。
#   如果案主曾經把這封信關掉，它會自己復活；而這種事沒有錯誤訊息。
#
# 這裡不用手動步驟，是因為手動步驟會被忘記，而忘記的後果是安靜的。
_RENAMED = {"host_assigned": "gm_assigned"}


def ensure(cur) -> None:
    """把還不存在的範本寫進去。已經存在的**不動**——那可能是案主改過的。

    ★ 所以改了這個檔案裡的預設內容之後，**已經上線的資料庫不會自動跟著變**。
      要套用就跑 reset()（見下），或把那一列刪掉等下次啟動重塞。
    """
    # 先遷移改過名的 key，再補齊缺的。順序不能反——反過來會先插一列新的，
    # 然後改名撞到主鍵。
    for old, new in _RENAMED.items():
        cur.execute(
            "UPDATE notification_template SET key = %s "
            "WHERE key = %s AND NOT EXISTS ("
            "  SELECT 1 FROM notification_template WHERE key = %s)",
            (new, old, new),
        )
    for key, (subject, body) in TEMPLATES.items():
        cur.execute(
            "INSERT INTO notification_template (key, subject, body_html) "
            "VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
            (key, subject, body),
        )


def reset(cur, *keys: str) -> list[str]:
    """把指定的範本**覆蓋**成這個檔案裡的預設值，回傳實際改到的 key。
    不給 keys 就是全部。

    ★ 這支會蓋掉案主用 SQL 改過的內容，所以刻意**不在啟動時呼叫**——
      ensure() 才是啟動時跑的那支。這支是給「改了程式碼的預設內容，要把
      它推到既有資料庫」用的，執行前先確認那幾列沒有你不想弄丟的修改：

          SELECT key, subject FROM notification_template;

    用法（在 backend 目錄）：

        ./.venv/bin/python -c "
        from dotenv import load_dotenv; load_dotenv('.env')
        import tenancy
        from db.connection import pool
        from db import default_templates as dt
        with tenancy.use(tenancy.for_cli()), pool.connection() as c, c.cursor() as cur:
            print(dt.reset(cur)); c.commit()"

    （有好幾家店時前面加 TENANT=<店家>。）
    """
    targets = keys or tuple(TEMPLATES)
    changed = []
    for key in targets:
        if key not in TEMPLATES:
            continue
        subject, body = TEMPLATES[key]
        cur.execute(
            "UPDATE notification_template SET subject = %s, body_html = %s "
            "WHERE key = %s AND (subject <> %s OR body_html <> %s)",
            (subject, body, key, subject, body),
        )
        if cur.rowcount:
            changed.append(key)
    return changed
