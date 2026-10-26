"""劇本簡介頁（/mmg/<id>）的資料：讀、寫、驗證。

## 這一頁是公開的

不用登入就看得到，網址可以貼到 LINE 群組或社群。內容分兩種：

  店家編輯的   封面圖、劇本簡介、角色（名稱＋介紹＋圖片，最多 12 個）、
               老闆的話、類型標籤（最多 8 個）
  自動帶入的   名稱、時長、人數、售價（平日／假日）、訂金、額外註記
               ——都是劇本本來就有的欄位，沒有值就不回（前端就不顯示）

★ 訂金只有「這家店有收訂金」（functions.deposit = on）時才回。不收訂金的店，
  簡介頁上不該出現一個「訂金 NT$ 0」。

★ 只公開**上架中**的劇本（status 是 active，或 preparing 但上架時間已到）。
  準備中、已下架的一律當作不存在（404）——還沒公開的劇本不該先被看到。
  跟玩家的劇本清單不同的是：預約截止（end_booking）過了照樣看得到簡介，
  因為那只代表「不再接預約」，這齣戲還在架上（見 db/listing.py）。

## 防攻擊放在哪

這裡只管資料。快取、限流、瀏覽紀錄的暫存都在 intro_public.py——公開頁面
最大的風險是被人拿來叫醒資料庫，那些機制的目的是讓大部分流量不碰到這裡。
"""

from __future__ import annotations

from db import functions, listing

# 字數上限。超過就擋下來並說清楚上限是多少，不要默默截斷——截斷的話，
# 店家貼了一大段、存檔成功，簡介頁卻少了後半段，他不會知道為什麼。
SYNOPSIS_MAX = 3000
OWNER_NOTE_MAX = 3000
# 角色：劇本是 4~10 人，留一點餘裕給 NPC。
CHARACTERS_MAX = 12
CHARACTER_NAME_MAX = 30
CHARACTER_DESCRIPTION_MAX = 1000
TAGS_MAX = 8
TAG_MAX_CHARS = 12

# 上架中（見檔案開頭）。跟 listing.VISIBLE_SQL 的差別是不看 end_booking。
ON_SHELF_SQL = (
    "(status = 'active' OR (status = 'preparing' AND start_booking <= %(now)s))"
)

_FIELDS = (
    "id, name, period, players, price, price_holiday, booking_cost, booking_cost_holiday, "
    "extra_note, intro_synopsis, intro_owner_note, intro_tags, cover_version, "
    "cover_image IS NOT NULL"
)


class IntroError(ValueError):
    """店家送來的內容不合規定。訊息直接給店家看。"""


def _payload(cur, row) -> dict:
    (mid, name, period, players, price, price_holiday, cost, cost_holiday,
     extra_note, synopsis, owner_note, tags, cover_version, has_cover) = row
    deposit = functions.deposit_required(cur)
    out = {
        "id": mid,
        "name": name,
        # NUMERIC → float，JSON 序列化不了 Decimal。
        "period": float(period) if period is not None else None,
        "players": players,
        "price": price,
        "price_holiday": price_holiday,
        "booking_cost": cost if deposit else None,
        "booking_cost_holiday": cost_holiday if deposit else None,
        "extra_note": extra_note,
        "synopsis": synopsis,
        "characters": _characters(cur, mid),
        "owner_note": owner_note,
        "tags": list(tags or []),
        # 圖片的網址只給相對路徑，前端自己接上後端主機與店家前綴。
        # 版本號在網址裡，換圖時網址就不同（見 schema 的 cover_version 說明）。
        "cover_path": f"/api/public/mmg/{mid}/cover/{cover_version}.webp" if has_cover else None,
    }
    return out


def _characters(cur, mmg_id: int) -> list[dict]:
    """這一齣的角色，照順序。圖片只給網址（同封面，前端自己接上後端主機）。"""
    cur.execute(
        "SELECT id, name, description, image IS NOT NULL, image_version FROM mmg_character "
        "WHERE mmg_id = %s ORDER BY position, id",
        (mmg_id,),
    )
    return [
        {
            "id": cid,
            "name": name,
            "description": description,
            "image_path": f"/api/public/mmg/{mmg_id}/characters/{cid}/{version}.webp" if has_image else None,
        }
        for cid, name, description, has_image, version in cur.fetchall()
    ]


def get_public(cur, mmg_id: int) -> dict | None:
    """上架中的劇本的簡介。不在架上或不存在回 None（呼叫端回 404）。"""
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT {_FIELDS} FROM mmg WHERE id = %(id)s AND {ON_SHELF_SQL}",
        {"id": mmg_id, "now": now},
    )
    row = cur.fetchone()
    return _payload(cur, row) if row else None


def get_for_admin(cur, mmg_id: int) -> dict | None:
    """管理員編輯用：不管上不上架都回，另外附上它現在公不公開。"""
    now = listing.now_taipei(cur)
    cur.execute(
        f"SELECT {_FIELDS}, {ON_SHELF_SQL} FROM mmg WHERE id = %(id)s",
        {"id": mmg_id, "now": now},
    )
    row = cur.fetchone()
    if row is None:
        return None
    out = _payload(cur, row[:-1])
    out["public"] = bool(row[-1])
    return out


def _text(data: dict, key: str, limit: int, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise IntroError(f"{label}必須是文字")
    # 統一換行符號：Windows 貼上來的 \r\n 存進去，前端顯示時會多一個空行。
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > limit:
        raise IntroError(f"{label}最多 {limit} 字（目前 {len(value)} 字）")
    # 空白存成 NULL，讓「沒有」只有一種表示法（同 mmg.url、mmg.extra_note）。
    return value or None


def _tags(data: dict) -> list[str]:
    raw = data.get("tags")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise IntroError("類型標籤必須是文字清單")
    tags: list[str] = []
    for t in raw:
        t = " ".join(t.split())          # 頭尾空白去掉、中間連續空白併成一個
        if not t or t in tags:           # 空的與重複的直接略過，不算錯
            continue
        if len(t) > TAG_MAX_CHARS:
            raise IntroError(f"類型標籤每個最多 {TAG_MAX_CHARS} 字（「{t}」有 {len(t)} 字）")
        tags.append(t)
    if len(tags) > TAGS_MAX:
        raise IntroError(f"類型標籤最多 {TAGS_MAX} 個（目前 {len(tags)} 個）")
    return tags


def update_text(cur, mmg_id: int, data: dict) -> bool:
    """存劇本簡介、角色、老闆的話、類型標籤。劇本不存在回 False。

    全部一起存（整份覆蓋）。編輯頁一次送全部，沒有「只改其中一個」的情境。
    """
    synopsis = _text(data, "synopsis", SYNOPSIS_MAX, "劇本簡介")
    owner_note = _text(data, "owner_note", OWNER_NOTE_MAX, "老闆的話")
    tags = _tags(data)
    characters = _character_list(data)
    cur.execute(
        "UPDATE mmg SET intro_synopsis = %s, intro_owner_note = %s, intro_tags = %s "
        "WHERE id = %s",
        (synopsis, owner_note, tags, mmg_id),
    )
    if cur.rowcount != 1:
        return False
    _save_characters(cur, mmg_id, characters)
    return True


def _character_list(data: dict) -> list[tuple[int | None, str, str | None]]:
    """[(既有角色的 id 或 None, 名稱, 介紹)]，照畫面上的順序。

    ★ 名稱必填：沒有名稱就不能有介紹或圖片（案主 2026-10-26）。整個空白的條目
      由前端丟掉；送到這裡還沒有名稱的，一律擋下來說是第幾個。
    """
    raw = data.get("characters")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(c, dict) for c in raw):
        raise IntroError("角色資料格式不對")
    if len(raw) > CHARACTERS_MAX:
        raise IntroError(f"角色最多 {CHARACTERS_MAX} 個（目前 {len(raw)} 個）")
    out = []
    for n, c in enumerate(raw, start=1):
        cid = c.get("id")
        if cid is not None and (not isinstance(cid, int) or isinstance(cid, bool)):
            raise IntroError("角色資料格式不對")
        name = _text(c, "name", CHARACTER_NAME_MAX, f"第 {n} 個角色的名稱")
        if name is None:
            raise IntroError(f"第 {n} 個角色沒有填角色名稱")
        name = " ".join(name.split())          # 名稱是一行字：換行、連續空白併成一個空白
        description = _text(c, "description", CHARACTER_DESCRIPTION_MAX, f"第 {n} 個角色的介紹")
        out.append((cid, name, description))
    ids = [cid for cid, _n, _d in out if cid is not None]
    if len(ids) != len(set(ids)):
        raise IntroError("角色資料格式不對")
    return out


def _save_characters(cur, mmg_id: int, characters: list[tuple[int | None, str, str | None]]) -> None:
    """整份覆蓋：清單裡沒有的角色刪掉（圖片一起），有 id 的更新，沒有的新增。"""
    keep = [cid for cid, _n, _d in characters if cid is not None]
    if keep:
        cur.execute("SELECT id FROM mmg_character WHERE mmg_id = %s AND id = ANY(%s)", (mmg_id, keep))
        if len(cur.fetchall()) != len(keep):
            # 別的分頁已經刪掉了這個角色，或 id 是別齣戲的。不要猜，請他重新整理。
            raise IntroError("角色資料已經在別的地方改過了，請重新整理頁面再編輯")
    cur.execute("DELETE FROM mmg_character WHERE mmg_id = %s AND NOT (id = ANY(%s))", (mmg_id, keep))
    for pos, (cid, name, description) in enumerate(characters):
        if cid is None:
            cur.execute(
                "INSERT INTO mmg_character (mmg_id, position, name, description) VALUES (%s, %s, %s, %s)",
                (mmg_id, pos, name, description),
            )
        else:
            cur.execute(
                "UPDATE mmg_character SET position = %s, name = %s, description = %s WHERE id = %s",
                (pos, name, description, cid),
            )


def set_cover(cur, mmg_id: int, webp: bytes | None) -> int | None:
    """換封面（webp=None 就是刪掉）。回傳新的版本號；劇本不存在回 None。

    ★ 刪掉也要加版本號：前端拿到的圖片網址帶著舊版本，不加的話瀏覽器會繼續
      顯示快取住的那張「已經刪掉的圖」。
    """
    cur.execute(
        "UPDATE mmg SET cover_image = %s, cover_version = cover_version + 1 "
        "WHERE id = %s RETURNING cover_version",
        (webp, mmg_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_cover(cur, mmg_id: int, version: int, *, public: bool) -> bytes | None:
    """取封面圖。版本對不上（舊網址）回 None。public=True 時只給上架中的劇本。"""
    sql = "SELECT cover_image FROM mmg WHERE id = %(id)s AND cover_version = %(v)s"
    params = {"id": mmg_id, "v": version}
    if public:
        sql += f" AND {ON_SHELF_SQL}"
        params["now"] = listing.now_taipei(cur)
    cur.execute(sql, params)
    row = cur.fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


def set_character_image(cur, mmg_id: int, character_id: int, webp: bytes | None) -> bool:
    """換角色圖（webp=None 就是刪掉），版本號一律加一（理由同 set_cover）。
    角色不存在或不是這一齣的回 False。"""
    cur.execute(
        "UPDATE mmg_character SET image = %s, image_version = image_version + 1 "
        "WHERE id = %s AND mmg_id = %s",
        (webp, character_id, mmg_id),
    )
    return cur.rowcount == 1


def get_character_image(cur, mmg_id: int, character_id: int, version: int, *, public: bool) -> bytes | None:
    """取角色圖。版本對不上回 None。public=True 時只給上架中的劇本（同 get_cover）。"""
    sql = (
        "SELECT c.image FROM mmg_character c JOIN mmg ON mmg.id = c.mmg_id "
        "WHERE c.id = %(cid)s AND c.mmg_id = %(id)s AND c.image_version = %(v)s"
    )
    params = {"cid": character_id, "id": mmg_id, "v": version}
    if public:
        sql += f" AND {ON_SHELF_SQL}"
        params["now"] = listing.now_taipei(cur)
    cur.execute(sql, params)
    row = cur.fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


def public_images(cur, mmg_id: int) -> list[tuple[tuple, bytes]]:
    """這一齣所有的圖（封面＋角色），給公開頁的快取預先放好（見 intro_public 第 2 點）。

    呼叫端已經確定這一齣在架上（get_public 有回東西）才呼叫。key 跟圖片路由用的一樣。
    """
    out: list[tuple[tuple, bytes]] = []
    cur.execute("SELECT cover_image, cover_version FROM mmg WHERE id = %s", (mmg_id,))
    row = cur.fetchone()
    if row and row[0] is not None:
        out.append((("cover", mmg_id, row[1]), bytes(row[0])))
    cur.execute(
        "SELECT id, image, image_version FROM mmg_character WHERE mmg_id = %s AND image IS NOT NULL",
        (mmg_id,),
    )
    out.extend((("character", mmg_id, cid, version), bytes(image)) for cid, image, version in cur.fetchall())
    return out


def insert_views(cur, rows: list[tuple]) -> int:
    """把暫存在記憶體的瀏覽紀錄一次寫進來。rows 是 (mmg_id, ip, forwarded_for,
    user_agent, viewed_at)。

    ★ 先過濾掉已經不存在的劇本：紀錄暫存的期間劇本可能被刪了，外鍵會讓整批
      寫入失敗——為了一筆舊紀錄丟掉一整批，不划算。
    """
    if not rows:
        return 0
    cur.execute("SELECT id FROM mmg WHERE id = ANY(%s)", (list({r[0] for r in rows}),))
    alive = {r[0] for r in cur.fetchall()}
    keep = [r for r in rows if r[0] in alive]
    if keep:
        cur.executemany(
            "INSERT INTO intro_view (mmg_id, ip, forwarded_for, user_agent, viewed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            keep,
        )
    return len(keep)


# 瀏覽紀錄保留幾天。隱私權政策（privacy.html）寫的是同一個數字，要改就一起改。
VIEW_RETENTION_DAYS = 90


def purge_views(cur) -> int:
    """清掉超過保留期限的瀏覽紀錄。每日排程呼叫。"""
    cur.execute(
        "DELETE FROM intro_view WHERE viewed_at < NOW() - make_interval(days => %s)",
        (VIEW_RETENTION_DAYS,),
    )
    return cur.rowcount
