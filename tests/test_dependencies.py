"""換套件版本時最容易悄悄壞掉的三個地方，各用一個測試釘住：

  - google-auth：登入時驗 Google ID Token（auth_utils/google_auth.py）。不連網路：自己簽
    一張 token，把「Google 的公開金鑰」換成自己的。
  - Flask-Cors：跨來源的回應標頭只給這家店自己的前端網址，長得像的不算。
  - gunicorn：Render 上的 Start Command（--workers 2 --threads 4 --timeout 60）在這一版
    還是同一個意思，而且 --threads 大於 1 時用的是多執行緒的 worker。
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

# ── google-auth ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def google_signer():
    """一把自己產生的 RSA 金鑰與它的憑證，代替 Google 的。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from google.auth import crypt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-signer")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return crypt.RSASigner.from_string(pem_key, key_id="test-kid"), cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture()
def google_token(google_signer, monkeypatch):
    from google.auth import jwt
    from google.oauth2 import id_token

    signer, cert = google_signer
    monkeypatch.setattr(id_token, "_fetch_certs", lambda request, url: {"test-kid": cert.decode()})

    def make(**claims):
        now = int(time.time())
        payload = {"iss": "https://accounts.google.com", "aud": "client-a.apps.googleusercontent.com",
                   "sub": "1234567890", "iat": now, "exp": now + 3600,
                   "email": "Player@Example.test", "email_verified": True, **claims}
        return jwt.encode(signer, payload).decode()

    return make


def _verify(token: str) -> str | None:
    import tenancy
    from auth_utils.google_auth import verify_google_id_token

    with tenancy.use("shop-a"):
        return verify_google_id_token(token)


def test_a_valid_google_token_gives_the_lowercased_email(app_module, google_token):
    assert _verify(google_token()) == "player@example.test"


@pytest.mark.parametrize("claims, why", [
    ({"aud": "client-b.apps.googleusercontent.com"}, "別家店的 OAuth client 簽給的"),
    ({"email_verified": False}, "Google 沒驗證過的 email"),
    ({"exp": int(time.time()) - 600}, "已經過期"),
    ({"iss": "https://evil.example.test"}, "不是 Google 簽發的"),
])
def test_google_tokens_that_must_be_refused(app_module, google_token, claims, why):
    assert _verify(google_token(**claims)) is None, why


def test_a_token_signed_by_another_key_is_refused(app_module, google_token, google_signer):
    token = google_token()
    head, body, _sig = token.split(".")
    other = google_token(sub="someone-else").split(".")[2]   # 同一把金鑰簽另一份內容
    assert _verify(f"{head}.{body}.{other}") is None


# ── Flask-Cors ─────────────────────────────────────────────────


def _preflight(client, origin: str, store: str = "shop-a"):
    return client.options(f"/v/{store}/api/me", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})


def test_cors_answers_this_stores_front_end(client):
    r = _preflight(client, "https://shop-a.example.test")
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://shop-a.example.test"
    assert r.headers["Access-Control-Max-Age"] == "3600"


@pytest.mark.parametrize("origin", [
    "https://shop-a-example.test",              # 點換成連字號：當成正規表示式的話會被放行
    "https://shop-a.example.test.evil.test",
    "https://evil.example.test",
])
def test_cors_does_not_answer_lookalike_origins(client, origin):
    assert "Access-Control-Allow-Origin" not in _preflight(client, origin).headers


def test_another_stores_front_end_is_refused(client):
    """CORS 名單是所有店家來源的聯集，「這個來源只能打它自己那家店」由 app.py 自己擋。"""
    assert _preflight(client, "https://shop-b.example.test", store="shop-a").status_code == 403


# ── gunicorn ───────────────────────────────────────────────────


def test_the_start_command_still_means_two_threaded_workers():
    from gunicorn.config import Config

    cfg = Config()
    args = cfg.parser().parse_args(
        ["--workers", "2", "--threads", "4", "--timeout", "60", "--bind", "0.0.0.0:10000", "app:app"])
    for key, value in vars(args).items():
        if value is not None and key in cfg.settings:
            cfg.set(key, value)
    assert (cfg.workers, cfg.threads, cfg.timeout, cfg.bind) == (2, 4, 60, ["0.0.0.0:10000"])
    assert cfg.worker_class_str == "gthread", "threads 大於 1 卻是單執行緒的 worker，同時只能處理 2 個請求"
