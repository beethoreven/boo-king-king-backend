"""掃整個後端，找出直接拿 users.status 的字比對的 SQL。

停權到期之後欄位最多會落後七天（理由見 auth_utils/user_status.py），所以
「這個人現在算不算啟用」一律要用 user_status.active_sql()／effective_status_sql()。
這個測試把「記得要用換算後的狀態」變成「忘了就會失敗」。

會被抓的：SQL 字串裡出現 `status = 'active'`（或 <>、!=，或 'banned'／'deactive'），
UPDATE 的 `SET status = ...` 除外。

★ mmg 也有 status 欄位，也有 'active' 這個值，掃描分不出是哪一張表——那些
  加進 ALLOWED，**一定要寫理由**。ALLOWED 裡的項目已經不存在時測試也會失敗，
  避免清單過期。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PATTERN = re.compile(r"(?:\b\w+\.)?\bstatus\s*(?:=|<>|!=)\s*'(?:active|banned|deactive)'")

# (檔案相對路徑, 比對到的那一段) -> 為什麼可以讀欄位的字
ALLOWED: dict[tuple[str, str], str] = {
    ("auth_utils/user_status.py", "status = 'banned'"): "換算本身。",
    ("auth_utils/guard.py", "status = 'banned'"): (
        "每週排程把已到期的停權寫回資料庫（decay_points）。它要找的正是"
        "「欄位還寫 banned、事實已經是 active」的那些列。"
    ),
    ("db/mmg.py", "status = 'active'"): "mmg（劇本）的上架狀態，不是 users。",
    ("db/listing.py", "status = 'active'"): "mmg（劇本）的上架狀態，不是 users。",
}


def _python_files() -> list[Path]:
    skip = {"tests", "venv", ".venv", "__pycache__"}
    return [p for p in ROOT.rglob("*.py") if not skip & set(p.relative_to(ROOT).parts)]


def _sql_strings(tree: ast.AST):
    """所有字串常數，docstring 除外（說明文字裡寫 status = 'active' 是在講道理）。"""
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.value


def _hits() -> set[tuple[str, str]]:
    found = set()
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        for text in _sql_strings(ast.parse(path.read_text(), filename=rel)):
            for m in _PATTERN.finditer(text):
                if re.search(r"\bSET\s*$", text[: m.start()]):
                    continue
                found.add((rel, m.group(0)))
    return found


def test_user_status_is_read_through_one_formula():
    bad = sorted(_hits() - set(ALLOWED))
    assert not bad, (
        "這些 SQL 直接比 users.status 的字，停權到期的人會被當成仍在停權中。"
        "改用 auth_utils.user_status.active_sql()；如果比的不是 users，"
        "加進 ALLOWED 並寫理由：\n" + "\n".join(f"  {f}: {s}" for f, s in bad)
    )


def test_allowed_list_is_not_stale():
    stale = sorted(set(ALLOWED) - _hits())
    assert not stale, "ALLOWED 裡這些已經找不到了，請移除：\n" + "\n".join(
        f"  {f}: {s}" for f, s in stale
    )


# ── 實際跑一次：停權到期的主持人，每一處都要當他是啟用的 ──────────────

def test_expired_ban_counts_as_active_everywhere(app_module, tokens, pg_cluster):
    import psycopg

    import tenancy
    from admin_utils import mmg_admin, users_admin
    from line_utils import handler

    with tenancy.use("shop-b"), psycopg.connect(pg_cluster["shop_b"]) as conn, conn.cursor() as cur:
        rows = {}
        for label, until in (("expired", "NOW() - interval '1 hour'"),
                             ("current", "NOW() + interval '1 hour'")):
            cur.execute(
                "INSERT INTO users (email, name, role, status, banned_until, line_user_id) "
                f"VALUES (%s, %s, 2, 'banned', {until}, %s) RETURNING id",
                (f"gm-{label}@shop.test", f"主持人{label}", f"U-{label}"),
            )
            rows[label] = cur.fetchone()[0]
        cur.execute("UPDATE roles SET line_notification = true WHERE id = 2")

        candidates = {c["id"] for c in mmg_admin.list_gm_candidates(cur)}
        listed = {u["id"]: u["status"] for u in users_admin.list_all(cur)}
        pushed = set(handler.push_targets(cur))

        assert rows["expired"] in candidates and rows["current"] not in candidates
        assert handler.is_staff(cur, "U-expired") and not handler.is_staff(cur, "U-current")
        assert "U-expired" in pushed and "U-current" not in pushed
        assert listed[rows["expired"]] == "active" and listed[rows["current"]] == "banned"
        conn.rollback()
