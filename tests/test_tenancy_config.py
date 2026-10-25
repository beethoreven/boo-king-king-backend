"""店家設定（環境變數）的讀法。寫錯的設定一律起不來，而且訊息要說是哪一個。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.conftest import BACKEND

import tenancy

URL = "postgresql://u:secret-password@host/db"
URL_2 = "postgresql://u:other-password@host2/db"


def _multi(**extra) -> dict[str, str]:
    env = {
        "TENANTS": "eli,tutor-arthur",
        "ELI__DATABASE_URL": URL,
        "ELI__ALLOWED_ORIGINS": "https://eli.boo-king-king.workers.dev",
        "TUTOR_ARTHUR__DATABASE_URL": URL_2,
        "TUTOR_ARTHUR__ALLOWED_ORIGINS": "https://tutor-arthur.boo-king-king.workers.dev/",
    }
    env.update(extra)
    return env


def test_multi_store_format():
    tenants = tenancy.load(_multi(ELI__JOB_TOKEN="t", ELI__LINE_CHANNEL_SECRET="s",
                                  ELI__LINE_CHANNEL_ACCESS_TOKEN="a"))
    assert list(tenants) == ["eli", "tutor-arthur"]
    assert tenants["eli"].line_enabled and not tenants["tutor-arthur"].line_enabled
    # 結尾斜線會被拿掉——那是 README 一直提醒要注意的那個坑。
    assert tenants["tutor-arthur"].allowed_origins == ("https://tutor-arthur.boo-king-king.workers.dev",)


def test_single_store_format_keeps_the_old_behaviour():
    tenants = tenancy.load({"VARIANT": "tutor-arthur-demo", "DATABASE_URL": URL})
    (t,) = tenants.values()
    assert t.id == "tutor-arthur-demo" and t.allowed_origins == ()   # 單店沒設來源：放行全部（原本的行為）


@pytest.mark.parametrize("env, message", [
    ({}, "沒有店家設定"),
    (_multi(VARIANT="eli"), "只能設一個"),
    (_multi(DATABASE_URL=URL), "沒前綴"),
    (_multi(ELI__ALLOWED_ORIGINS=""), "ALLOWED_ORIGINS 沒有設定"),
    (_multi(ELI__DATABSE_URL=URL), "不認得的店家設定 ELI__DATABSE_URL"),
    (_multi(SHOP__DATABASE_URL=URL), "SHOP 不在 TENANTS"),
    (_multi(TENANTS="eli,eli"), "重複"),
    (_multi(TENANTS="Eli"), "格式不對"),
    (_multi(ELI__ALLOWED_ORIGINS="https://eli.example/booking"), "不是合法的來源"),
    ({"VARIANT": "x"}, "DATABASE_URL 沒有設定"),
    # 照 .env 習慣加了引號：連線錯誤會把含密碼的字串印進 log，要在讀設定時擋
    (_multi(ELI__DATABASE_URL=f'"{URL}"'), "ELI__DATABASE_URL 的值前後有引號"),
    (_multi(ELI__GOOGLE_CLIENT_ID="'abc'"), "ELI__GOOGLE_CLIENT_ID 的值前後有引號"),
    (_multi(ELI__DATABASE_URL="host=x dbname=y"), "要以 postgresql:// 開頭"),
    # 複製貼上貼錯格：兩家店會完全合併
    (_multi(TUTOR_ARTHUR__DATABASE_URL=URL), "ELI__DATABASE_URL 跟 TUTOR_ARTHUR__DATABASE_URL 是同一個值"),
    (_multi(ELI__LINE_CHANNEL_SECRET="s", ELI__LINE_CHANNEL_ACCESS_TOKEN="a1",
            TUTOR_ARTHUR__LINE_CHANNEL_SECRET="s", TUTOR_ARTHUR__LINE_CHANNEL_ACCESS_TOKEN="a2"),
     "LINE_CHANNEL_SECRET 是同一個值"),
])
def test_bad_config_refuses_to_start(env, message):
    with pytest.raises(tenancy.ConfigError, match=message) as err:
        tenancy.load(env)
    assert "secret-password" not in str(err.value), "錯誤訊息不能帶出密碼"


def test_secrets_never_show_up_in_repr():
    t = tenancy.load(_multi(ELI__JOB_TOKEN="job-secret", ELI__LINE_CHANNEL_SECRET="line-secret",
                            ELI__LINE_CHANNEL_ACCESS_TOKEN="line-access"))["eli"]
    text = repr(t)
    for secret in ("secret-password", "job-secret", "line-secret", "line-access"):
        assert secret not in text


def test_single_store_service_still_boots(pg_cluster):
    """demo 用的就是單店寫法。換成多店家程式之後，它要能照原本的環境變數起來。"""
    env = {**os.environ, "VARIANT": "solo", "DATABASE_URL": pg_cluster["shop_a"], "TENANTS": "",
           "ALLOWED_ORIGINS": "", "GOOGLE_CLIENT_ID": "", "JOB_TOKEN": "solo-token",
           "LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_ACCESS_TOKEN": "", "BREVO_API_KEY": ""}
    for key in list(env):
        if "__" in key:
            del env[key]
    code = (
        "import app\n"
        "c = app.app.test_client()\n"
        "t = {'X-Job-Token': 'solo-token'}\n"
        "print(c.get('/api/health').status_code, c.get('/v/solo/api/ready', headers=t).status_code,"
        " c.get('/v/other/api/ready', headers=t).status_code,"
        " c.post('/v/solo/api/jobs/weekly', headers={'X-Job-Token': 'solo-token'}).status_code)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().splitlines()[-1] == "200 200 404 200"
