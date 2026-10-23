import base64
import hashlib
import os
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
    token_path(tmp_path).chmod(0o600)
    with pytest.raises(XOAuth2Error, match="壊れている"):
        load_token(tmp_path)


def test_load_token_semantic_corruption_fails_fast(tmp_path: Path) -> None:
    save_token(tmp_path, make_token(expires_at="not-a-date"))
    with pytest.raises(XOAuth2Error, match="壊れている"):
        load_token(tmp_path)


def test_load_token_rejects_loose_permissions(tmp_path: Path) -> None:
    save_token(tmp_path, make_token())
    token_path(tmp_path).chmod(0o644)
    with pytest.raises(XOAuth2Error, match="パーミッション"):
        load_token(tmp_path)


def test_load_token_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    token_path(tmp_path).symlink_to(real)
    with pytest.raises(XOAuth2Error, match="symlink"):
        load_token(tmp_path)


def test_load_token_rejects_naive_expires_at_and_missing_scope(tmp_path: Path) -> None:
    # naive 日時は失効判定の aware 比較で TypeError になる前に load で拒否する
    save_token(tmp_path, make_token(expires_at="2026-07-13T14:00:00"))
    with pytest.raises(XOAuth2Error, match="壊れている"):
        load_token(tmp_path)
    # 必要 scope を欠く保存済み token も再認可へ誘導(S1b の取込だけが失敗する事故防止)
    save_token(
        tmp_path,
        XOAuth2Token(
            access_token="at", refresh_token="rt",
            expires_at="2026-07-13T14:00:00Z", scope="users.read offline.access",
        ),
    )
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
        refresh_token_grant(
            client, client_id="cid", client_secret=None,
            refresh_token="rt", fallback_scope=" ".join(OAUTH2_SCOPES),
        )


def test_scope_shrink_rejected_without_leaking(tmp_path: Path) -> None:
    # scope 縮小を「認可完了」で保存しない(S1b の取込だけが後で失敗する事故防止)
    body = {**token_response(1), "scope": "users.read offline.access"}
    client = httpx.Client(transport=capture_transport([], body))
    with pytest.raises(XOAuth2Error, match="bookmark.read"):
        exchange_code(
            client, client_id="cid", client_secret=None,
            code="c", redirect_uri="r", code_verifier="v",
        )


def test_refresh_response_without_scope_keeps_stored_scope() -> None:
    body = {k: v for k, v in token_response(2).items() if k != "scope"}
    client = httpx.Client(transport=capture_transport([], body))
    token = refresh_token_grant(
        client, client_id="cid", client_secret=None,
        refresh_token="rt", fallback_scope=" ".join(OAUTH2_SCOPES),
    )
    assert token.scope == " ".join(OAUTH2_SCOPES)  # 省略時は検証済みの値を維持


def test_error_body_not_reflected_in_exception() -> None:
    # エラー本文は untrusted(gateway が要求内容を任意フィールドに反射しうる)。
    # 既知の OAuth error code の allowlist 一致以外は固定文言(error 欄自体の反射も遮断)
    sentinel = "SECRET-REFRESH-TOKEN-VALUE"
    for body in (
        {"error": "invalid_grant", "error_description": f"bad token {sentinel}"},
        {"error": sentinel},  # error 欄そのものに反射されるケース
        {"title": sentinel},
    ):
        client = httpx.Client(transport=capture_transport([], body, status=400))
        with pytest.raises(XOAuth2Error) as exc_info:
            exchange_code(
                client, client_id="cid", client_secret=None,
                code="c", redirect_uri="r", code_verifier="v",
            )
        assert sentinel not in str(exc_info.value)
    # allowlist 一致は code を表示する(診断可能性は保つ)
    client = httpx.Client(
        transport=capture_transport([], {"error": "invalid_grant"}, status=400)
    )
    with pytest.raises(XOAuth2Error, match="invalid_grant"):
        exchange_code(
            client, client_id="cid", client_secret=None,
            code="c", redirect_uri="r", code_verifier="v",
        )


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


def test_session_401_on_last_retry_attempt_still_refreshes_and_resends(tmp_path: Path) -> None:
    # 500→500→401(リトライ予算の最終試行で 401)でも refresh + 再送が走る(SF81-003)
    save_token(tmp_path, make_token())
    statuses = [500, 500, 401]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls.append("refresh")
            return httpx.Response(200, json=token_response(2))
        calls.append("get")
        if statuses:
            return httpx.Response(statuses.pop(0))
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    session = make_session(tmp_path, httpx.MockTransport(handler), make_token())
    assert fetch_me(session) == "kosako"
    assert calls == ["get", "get", "get", "refresh", "get"]


def test_session_401_after_refresh_returns_401_without_loop(tmp_path: Path) -> None:
    save_token(tmp_path, make_token())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls.append("refresh")
            return httpx.Response(200, json=token_response(2))
        calls.append("get")
        return httpx.Response(401)

    session = make_session(tmp_path, httpx.MockTransport(handler), make_token())
    assert session.get(USERS_ME_URL).status_code == 401  # 恒久 401 はそのまま返す
    assert calls == ["get", "refresh", "get"]  # refresh は1回だけ(ループしない)


def test_refresh_adopts_token_rotated_by_other_process(tmp_path: Path) -> None:
    # 他プロセス(朝バッチ ⟷ doctor)が先に refresh 済みなら、使用済み refresh token を
    # 再送して invalid_grant にせず、ディスクの新 token を採用する(SF81-002)
    rotated = XOAuth2Token(
        access_token="at-2", refresh_token="rt-2",
        expires_at="2026-07-13T14:00:00Z", scope=" ".join(OAUTH2_SCOPES),
    )
    save_token(tmp_path, rotated)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls.append("refresh")  # 呼ばれてはいけない
            return httpx.Response(400, json={"error": "invalid_grant"})
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    stale = make_token(expires_at="2026-07-13T11:00:00Z")  # 失効済みの旧 token(rt-1)
    session = make_session(tmp_path, httpx.MockTransport(handler), stale)
    assert fetch_me(session) == "kosako"
    assert calls == ["Bearer at-2"]  # ネットワーク refresh なしで新 token を採用


def test_refresh_adopts_rotated_expiring_token_and_refreshes_with_it(tmp_path: Path) -> None:
    # 他プロセスの新 token 自体も失効間近なら、旧(使用済み)でなく新 refresh token で
    # refresh を続行する(旧を再送すると invalid_grant で認可を失う)
    rotated_expiring = XOAuth2Token(
        access_token="at-2", refresh_token="rt-2",
        expires_at="2026-07-13T12:01:00Z", scope=" ".join(OAUTH2_SCOPES),  # margin 内
    )
    save_token(tmp_path, rotated_expiring)
    sent_refresh_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            form = dict(parse_qsl(request.content.decode()))
            sent_refresh_tokens.append(form["refresh_token"])
            return httpx.Response(200, json=token_response(3))
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    stale = make_token(expires_at="2026-07-13T11:00:00Z")  # 失効済みの旧 token(rt-1)
    session = make_session(tmp_path, httpx.MockTransport(handler), stale)
    assert fetch_me(session) == "kosako"
    assert sent_refresh_tokens == ["rt-2"]  # 使用済みの rt-1 を再送しない
    assert load_token(tmp_path).refresh_token == "rt-3"


def test_token_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    import fcntl

    from app.connectors.x_oauth2 import LOCK_FILENAME, _token_lock

    lock_file = tmp_path / LOCK_FILENAME
    with _token_lock(tmp_path):
        fd = os.open(lock_file, os.O_WRONLY)
        with pytest.raises(BlockingIOError):  # 保持中は他者が取れない(排他)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
    fd = os.open(lock_file, os.O_WRONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 解放後は取れる
    os.close(fd)


def test_refresh_failure_releases_lock_for_next_attempt(tmp_path: Path) -> None:
    statuses = [400]  # 初回 refresh は失敗、次回は成功

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            if statuses:
                return httpx.Response(statuses.pop(0), json={"error": "server_error"})
            return httpx.Response(200, json=token_response(2))
        return httpx.Response(200, json={"data": {"username": "kosako"}})

    expired = make_token(expires_at="2026-07-13T11:00:00Z")
    session = make_session(tmp_path, httpx.MockTransport(handler), expired)
    with pytest.raises(XOAuth2Error, match="再認可"):
        session.get(USERS_ME_URL)
    assert fetch_me(session) == "kosako"  # 失敗後もロックが解放されており続行できる


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
    # 実行環境の proxy 設定(HTTP_PROXY 等)を使わず、loopback の callback へ直接つなぐ(GH#258)
    with httpx.Client(trust_env=False) as client:
        return client.get(url, timeout=5)


def run_callback(server: CallbackServer, results: dict) -> threading.Thread:
    def target() -> None:
        try:
            results["code"] = server.wait_for_code(timeout_s=10)
        except XOAuth2Error as e:
            results["error"] = str(e)

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_callback_server_returns_code_and_ignores_other_paths() -> None:
    server = CallbackServer(0, "st")  # テストは空きポート(実フローは config の固定ポート)
    results: dict = {}
    thread = run_callback(server, results)
    hit(f"http://127.0.0.1:{server.port}/favicon.ico")  # ブラウザのノイズは無視
    response = hit(f"http://127.0.0.1:{server.port}/callback?code=abc&state=st")
    thread.join(timeout=10)
    assert results["code"] == "abc"
    assert response.status_code == 200


def test_callback_server_ignores_forged_state_and_accepts_valid_one() -> None:
    # 偽 callback(state 不一致)は 400 で無視し、フローを中断・確定させない(SF81-007)
    server = CallbackServer(0, "expected")
    results: dict = {}
    thread = run_callback(server, results)
    forged = hit(f"http://127.0.0.1:{server.port}/callback?code=evil&state=forged")
    denial = hit(f"http://127.0.0.1:{server.port}/callback?error=denied&state=forged")
    response = hit(f"http://127.0.0.1:{server.port}/callback?code=abc&state=expected")
    thread.join(timeout=10)
    assert forged.status_code == 400 and denial.status_code == 400
    assert response.status_code == 200
    assert results["code"] == "abc"  # 偽 code は採用されない


def test_callback_server_surfaces_denial_with_valid_state() -> None:
    server = CallbackServer(0, "st")
    results: dict = {}
    thread = run_callback(server, results)
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
