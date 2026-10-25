"""兩家測試店的共用設定：一個臨時的本機 Postgres，裡面兩個資料庫。

★ 不碰任何真的 Neon 專案。每次測試都 initdb 一個全新的叢集，結束時關掉並
  刪掉——不會留下背景的資料庫行程。

需要本機有 Postgres 的伺服器程式（initdb、pg_ctl）。macOS 用 Homebrew 的
postgresql@18 就有。找不到的話跨店測試會整批略過並說明原因；掃描型的測試
（test_tenant_state.py）不需要資料庫，照樣會跑。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

# 兩家店的設定。刻意讓它們「長得像」：同一個 email 在兩邊都有、劇本編號
# 都從 1 開始——正是會撞在一起的情況。
STORES = {
    "shop-a": {
        "google_client_id": "client-a.apps.googleusercontent.com",
        "origin": "https://shop-a.example.test",
        "job_token": "job-token-a",
        "line_secret": "line-secret-a",
        "line_token": "line-access-a",
    },
    "shop-b": {
        "google_client_id": "client-b.apps.googleusercontent.com",
        "origin": "https://shop-b.example.test",
        "job_token": "job-token-b",
        "line_secret": "line-secret-b",
        "line_token": "line-access-b",
    },
}


def _find(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    for candidate in ("/usr/local/bin", "/opt/homebrew/bin",
                      "/usr/local/opt/postgresql@18/bin", "/opt/homebrew/opt/postgresql@18/bin"):
        path = Path(candidate) / binary
        if path.exists():
            return str(path)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_cluster():
    """起一個臨時的 Postgres 叢集，回傳 {資料庫名: 連線字串}。"""
    initdb, pg_ctl = _find("initdb"), _find("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("本機沒有 Postgres 伺服器程式（initdb / pg_ctl），跨店測試略過。")

    # 路徑要短：unix socket 的路徑在 macOS 上限約 100 字元。
    root = Path(tempfile.mkdtemp(prefix="bkk-pg-", dir="/tmp"))
    data = root / "data"
    port = _free_port()
    # ★ macOS 上沒設 LC_ALL 的話，postmaster 啟動時會因為
    #   「became multithreaded during startup」直接結束（2026-10-25 實測）。
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    # ★ 從 mkdtemp 之後的每一步都在 try 裡：任何一步失敗（包括 initdb 本身），
    #   finally 都會把叢集關掉、資料夾刪掉，不留東西在 /tmp。
    try:
        subprocess.run(
            [initdb, "-D", str(data), "-U", "postgres", "-A", "trust", "--no-sync",
             "-E", "UTF8", "--locale=C"],
            check=True, capture_output=True, env=env,
        )
        started = subprocess.run(
            [pg_ctl, "-D", str(data), "-l", str(root / "log"), "-w", "-o",
             f"-p {port} -k {root} -c listen_addresses='' -c fsync=off", "start"],
            capture_output=True, env=env,
        )
        if started.returncode != 0:
            log = (root / "log").read_text(errors="replace") if (root / "log").exists() else ""
            raise RuntimeError(f"臨時 Postgres 起不來：\n{log[-2000:]}")

        import psycopg

        admin = f"postgresql://postgres@/postgres?host={root}&port={port}"
        with psycopg.connect(admin, autocommit=True) as conn:
            for name in ("shop_a", "shop_b"):
                conn.execute(f"CREATE DATABASE {name}")
        yield {
            name: f"postgresql://postgres@/{name}?host={root}&port={port}"
            for name in ("shop_a", "shop_b")
        }
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True, env=env)
        shutil.rmtree(root, ignore_errors=True)


def multi_store_env(urls: dict[str, str]) -> dict[str, str]:
    """多店家寫法的環境變數。沒前綴的每店設定一律設成空字串——那樣
    app.py 的 load_dotenv 就不會從本機 .env 補進單店的值（它不覆蓋已存在的鍵）。"""
    env = {
        "TENANTS": "shop-a,shop-b",
        "VARIANT": "",
        "DATABASE_URL": "",
        "GOOGLE_CLIENT_ID": "",
        "ALLOWED_ORIGINS": "",
        "JOB_TOKEN": "",
        "LINE_CHANNEL_SECRET": "",
        "LINE_CHANNEL_ACCESS_TOKEN": "",
        "BREVO_API_KEY": "",
    }
    for store, db in (("shop-a", "shop_a"), ("shop-b", "shop_b")):
        cfg = STORES[store]
        p = store.upper().replace("-", "_") + "__"
        env.update({
            p + "DATABASE_URL": urls[db],
            p + "GOOGLE_CLIENT_ID": cfg["google_client_id"],
            p + "ALLOWED_ORIGINS": cfg["origin"],
            p + "JOB_TOKEN": cfg["job_token"],
            p + "LINE_CHANNEL_SECRET": cfg["line_secret"],
            p + "LINE_CHANNEL_ACCESS_TOKEN": cfg["line_token"],
        })
    return env


@pytest.fixture(scope="session")
def app_module(pg_cluster):
    """用兩家店的設定 import app（只 import 一次，整個測試階段共用）。"""
    for key, value in multi_store_env(pg_cluster).items():
        os.environ[key] = value
    sys.path.insert(0, str(BACKEND))
    import app as app_mod  # noqa: PLC0415 - 一定要在設好環境變數之後

    return app_mod


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()


# 每家店的 session token 由 seeded 在那家店裡用 sessions.create_session 發
# （token 帶著那家店的簽章，見 auth_utils/sessions.py），測試用 tokens 取。
#   a_admin       A 店：admin@shop.test，管理員
#   a_player      A 店：player@shop.test，一般玩家
#   b_same_email  B 店：admin@shop.test，在 B 店只是一般玩家
#   b_owner       B 店：owner@shop.test，管理員


def _seed(store: str, url: str, *, users, sessions, mmg_name, site_name, gm_mode) -> dict:
    import psycopg
    import tenancy
    from auth_utils import sessions as sessions_mod

    tokens = {}
    with tenancy.use(store), psycopg.connect(url) as conn, conn.cursor() as cur:
        for email, name, role in users:
            cur.execute(
                "INSERT INTO users (email, name, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (email) DO NOTHING",
                (email, name, role),
            )
        for label, email in sessions:
            tokens[label] = sessions_mod.create_session(cur, email)
        cur.execute("SELECT count(*) FROM mmg")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO mmg (name) VALUES (%s)", (mmg_name,))
        cur.execute("UPDATE settings SET value = %s WHERE key = 'site_name'", (site_name,))
        cur.execute("UPDATE functions SET value = %s WHERE key = 'gm_mode'", (gm_mode,))
        conn.commit()
    return tokens


@pytest.fixture(scope="session")
def tokens(app_module, pg_cluster):
    return {
        **_seed(
            "shop-a", pg_cluster["shop_a"],
            users=[("admin@shop.test", "A管理員", 1), ("player@shop.test", "A玩家", 3)],
            sessions=[("a_admin", "admin@shop.test"), ("a_player", "player@shop.test")],
            mmg_name="A店的劇本", site_name="A店", gm_mode="single",
        ),
        **_seed(
            "shop-b", pg_cluster["shop_b"],
            users=[("owner@shop.test", "B管理員", 1), ("admin@shop.test", "在B只是玩家", 3)],
            sessions=[("b_same_email", "admin@shop.test"), ("b_owner", "owner@shop.test")],
            mmg_name="B店的劇本", site_name="B店", gm_mode="assign",
        ),
    }
