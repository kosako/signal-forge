import base64
import hashlib
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from app.connectors.x_oauth2 import (
    OAUTH2_SCOPES,
    TOKEN_URL,
    USERS_ME_URL,
    CallbackServer,
    XOAuth2Error,
    XOAuth2Session,
    XOAuth2Token,
    build_authorize_url,
    exchange_code,
    fetch_me,
    generate_pkce,
    load_token,
    refresh_token_grant,
    run_authorization_flow,
    save_token,
    token_path,
)

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def make_token(expires_at: str = "2026-07-13T14:00:00Z") -> XOAuth2Token:
    return XOAuth2Token(
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=expires_at,
        scope=" ".join(OAUTH2_SCOPES),
    )


def token_response(n: int) -> dict:
    return {
        "access_token": f"at-{n}",
        "refresh_token": f"rt-{n}",
        "expires_in": 7200,
        "scope": " ".join(OAUTH2_SCOPES),
        "token_type": "bearer",
    }


# --- PKCE / 認可 URL ---


def test_pkce_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128  # RFC 7636 の値域
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected
    assert "=" not in challenge  # padding なし(base64url)


def test_authorize_url_contains_required_params() -> None:
    url = build_authorize_url("cid", "http://127.0.0.1:8791/callback", "st", "ch")
    parsed = urlsplit(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert parsed.scheme == "https"
    assert params["response_type"] == "code"
    assert params["client_id"] == "cid"
    assert params["redirect_uri"] == "http://127.0.0.1:8791/callback"
    assert params["state"] == "st"
    assert params["code_challenge"] == "ch"
    assert params["code_challenge_method"] == "S256"
    # 取込に必要な全 scope + refresh 用 offline.access(2h 失効のため無人 run に必須)
    assert set(params["scope"].split()) == set(OAUTH2_SCOPES)


# --- token の永続化 ---


def test_token_save_load_roundtrip_with_0600(tmp_path: Path) -> None:
    token = make_token()
    save_token(tmp_path, token)
    path = token_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_token(tmp_path) == token
    assert not list(tmp_path.glob("*.tmp"))  # 一時ファイルを残さない


def test_load_token_absent_returns_none(tmp_path: Path) -> None:
    assert load_token(tmp_path) is None


def test_load_token_corrupted_fails_fast(tmp_path: Path) -> None:
    token_path(tmp_path).write_text('{"access_token": "at"}', encoding="utf-8")  # キー欠落
    with pytest.raises(XOAuth2Error, match="壊れている"):
        load_token(tmp_path)


# --- token エンドポイント ---


def capture_transport(captured: list[httpx.Request], response_body: dict, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=response_body)

    return httpx.MockTransport(handler)


def test_exchange_code_public_client_sends_client_id_in_body() -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(transport=capture_transport(captured, token_response(1)))
    token = exchange_code(
        client,
        client_id="cid",
        client_secret=None,
        code="the-code",
        redirect_uri="http://127.0.0.1:8791/callback",
        code_verifier="the-verifier",
    )
    assert token.access_token == "at-1" and token.refresh_token == "rt-1"
    request = captured[0]
    assert str(request.url) == TOKEN_URL
    form = dict(parse_qsl(request.content.decode()))
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["code_verifier"] == "the-verifier"
    assert form["client_id"] == "cid"  # public client は body
    assert "Authorization" not in request.headers


def test_exchange_code_confidential_client_uses_basic_auth() -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(transport=capture_transport(captured, token_response(1)))
    exchange_code(
        client,
        client_id="cid",
        client_secret="sec",
        code="c",
        redirect_uri="r",
        code_verifier="v",
    )
    request = captured[0]
    basic = base64.b64encode(b"cid:sec").decode()
    assert request.headers["Authorization"] == f"Basic {basic}"
    form = dict(parse_qsl(request.content.decode()))
    assert "client_id" not in form  # confidential は Basic のみ


def test_missing_refresh_token_in_response_rejected() -> None:
    body = {"access_token": "at", "expires_in": 7200}  # offline.access 欠落の応答
    client = httpx.Client(transport=capture_transport([], body))
    with pytest.raises(XOAuth2Error, match="offline.access"):
        refresh_token_grant(client, client_id="cid", client_secret=None, refresh_token="rt")


def test_token_endpoint_error_surfaces_status() -> None:
    client = httpx.Client(
        transport=capture_transport([], {"error": "invalid_request"}, status=400)
    )
    with pytest.raises(XOAuth2Error, match="400"):
        exchange_code(
            client, client_id="cid", client_secret=None,
            code="c", redirect_uri="r", code_verifier="v",
        )


# --- セッション(refresh・リトライ) ---


def make_session(tmp_path: Path, transport: httpx.MockTransport, token: XOAuth2Token):
    return XOAuth2Session(
        client=httpx.Client(transport=transport),
        config_dir=tmp_path,
        client_id="cid",
        client_secret=None,
        token=token,
        sleep=lambda _s: None,
        now=lambda: NOW,
    )


def test_session_refreshes_before_expiry_and_persists_rotation(tmp_path: Path) -> None:
    save_token(tmp_path, make_token())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls.append("refresh")
            return httpx.Response(200, json=token_response(2))
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    # 失効 2 分前(margin 5 分の内側)→ 先回り refresh が走る
    token = make_token(expires_at="2026-07-13T12:02:00Z")
    session = make_session(tmp_path, httpx.MockTransport(handler), token)
    assert fetch_me(session) == "kosako"
    assert calls == ["refresh", "Bearer at-2"]
    assert load_token(tmp_path).refresh_token == "rt-2"  # ローテーションを永続化


def test_session_retries_after_401_with_single_refresh(tmp_path: Path) -> None:
    save_token(tmp_path, make_token())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls.append("refresh")
            return httpx.Response(200, json=token_response(2))
        calls.append("get")
        if len([c for c in calls if c == "get"]) == 1:
            return httpx.Response(401)  # margin 外でも失効しうる
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    session = make_session(tmp_path, httpx.MockTransport(handler), make_token())
    assert fetch_me(session) == "kosako"
    assert calls == ["get", "refresh", "get"]


def test_session_refresh_failure_guides_reauth(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    token = make_token(expires_at="2026-07-13T11:00:00Z")  # 失効済み
    session = make_session(tmp_path, httpx.MockTransport(handler), token)
    with pytest.raises(XOAuth2Error, match="x-auth で再認可"):
        session.get(USERS_ME_URL)


def test_session_returns_429_without_retry(tmp_path: Path) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429)

    session = make_session(tmp_path, httpx.MockTransport(handler), make_token())
    assert session.get(USERS_ME_URL).status_code == 429
    assert len(attempts) == 1  # rate limit は待たず打ち切る(設計 02 §2 と同方針)


def test_session_retries_5xx_then_returns(tmp_path: Path) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500 if len(attempts) == 1 else 200, json={"data": {}})

    session = make_session(tmp_path, httpx.MockTransport(handler), make_token())
    assert session.get(USERS_ME_URL).status_code == 200
    assert len(attempts) == 2


def test_session_load_without_token_guides_auth(tmp_path: Path) -> None:
    with pytest.raises(XOAuth2Error, match="x-auth"):
        XOAuth2Session.load(
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            config_dir=tmp_path,
            client_id="cid",
            client_secret=None,
        )


# --- callback サーバ ---


def hit(url: str) -> httpx.Response:
    with httpx.Client() as client:
        return client.get(url, timeout=5)


def run_callback(server: CallbackServer, state: str, results: dict) -> threading.Thread:
    def target() -> None:
        try:
            results["code"] = server.wait_for_code(state, timeout_s=10)
        except XOAuth2Error as e:
            results["error"] = str(e)

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_callback_server_returns_code_and_ignores_other_paths() -> None:
    server = CallbackServer(0)  # テストは空きポート(実フローは config の固定ポート)
    results: dict = {}
    thread = run_callback(server, "st", results)
    hit(f"http://127.0.0.1:{server.port}/favicon.ico")  # ブラウザのノイズは無視
    response = hit(f"http://127.0.0.1:{server.port}/callback?code=abc&state=st")
    thread.join(timeout=10)
    assert results["code"] == "abc"
    assert response.status_code == 200


def test_callback_server_rejects_state_mismatch() -> None:
    server = CallbackServer(0)
    results: dict = {}
    thread = run_callback(server, "expected", results)
    hit(f"http://127.0.0.1:{server.port}/callback?code=abc&state=forged")
    thread.join(timeout=10)
    assert "state 不一致" in results["error"]


def test_callback_server_surfaces_denial() -> None:
    server = CallbackServer(0)
    results: dict = {}
    thread = run_callback(server, "st", results)
    hit(f"http://127.0.0.1:{server.port}/callback?error=access_denied&state=st")
    thread.join(timeout=10)
    assert "拒否" in results["error"]


# --- 認可フロー一気通貫(ブラウザの代わりに直接 callback を叩く) ---


def test_run_authorization_flow_end_to_end(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    client = httpx.Client(transport=capture_transport(captured, token_response(1)))
    echoed: list[str] = []

    def fake_browser(url: str) -> None:
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        # 別スレッドから、ユーザーの認可完了(redirect)を模す
        threading.Thread(
            target=lambda: hit(f"{params['redirect_uri']}?code=ok&state={params['state']}")
        ).start()

    token = run_authorization_flow(
        client,
        config_dir=tmp_path,
        client_id="cid",
        client_secret=None,
        callback_port=0,
        echo=echoed.append,
        open_browser=fake_browser,
        timeout_s=10,
    )
    assert token.access_token == "at-1"
    assert load_token(tmp_path) == token  # 保存済み
    form = dict(parse_qsl(captured[0].content.decode()))
    assert form["code"] == "ok"
    assert len(form["code_verifier"]) >= 43  # PKCE verifier が交換に使われている
