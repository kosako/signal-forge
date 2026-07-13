"""X OAuth 2.0(PKCE)認証基盤(S1 = GH#58 / ADR 0001「X ソースと認証」)。

ブックマーク取得(/2/users/:id/bookmarks)は OAuth 2.0 user context が API 上の必須で、
S1 の取込(ブックマーク・いいね)は認証経路を1本に保つため OAuth2 に統一する(ADR 0001)。

前提事実(API 実確認 2026-07-13):
- access token は offline.access を付けても 2h で失効する。無人朝バッチには
  refresh token(offline.access scope)が必須
- refresh token はローテーションされる(使用のたびに新 refresh token が発行される)
  前提で設計する。新 token の永続化を取りこぼすと認可チェーンが切れ、再認可
  (intel x-auth)が必要になるため、保存を先に行い原子的に書く

token ファイルは config_dir 直下・chmod 600(.env と同じ扱い。設計 05 §6)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from app.http import BACKOFF_S, MAX_RETRIES  # 共通リトライ規約(設計 05 §8)

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
USERS_ME_URL = "https://api.x.com/2/users/me"
# 取込(S1b: bookmarks / liked_tweets)+ refresh(offline.access)に必要な最小 scope
OAUTH2_SCOPES = ("tweet.read", "users.read", "bookmark.read", "like.read", "offline.access")
TOKEN_FILENAME = "x-oauth2-token.json"
ENV_CLIENT_SECRET = "X_OAUTH2_CLIENT_SECRET"  # confidential client のみ(.env・任意)
REFRESH_MARGIN_S = 300  # 失効のこの秒数前から先回りで refresh(バッチ途中の失効を避ける)
TIMEOUT_S = 30


class XOAuth2Error(Exception):
    """OAuth2 の認可・token 操作の失敗。再認可が必要なものはメッセージで案内する。"""


@dataclass(frozen=True)
class XOAuth2Token:
    access_token: str
    refresh_token: str  # offline.access 前提で常に保持(無い応答は保存前に拒否)
    expires_at: str  # UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'(DB・ログと同形式)
    scope: str


# --- token の永続化 ---


def token_path(config_dir: Path) -> Path:
    return config_dir / TOKEN_FILENAME


def load_token(config_dir: Path) -> XOAuth2Token | None:
    """保存済み token を返す。未認可(ファイル無し)は None。壊れたファイルは fail fast。"""
    path = token_path(config_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return XOAuth2Token(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
            scope=data["scope"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # 黙って再認可に倒さない(半端な上書き・手編集ミスの検出を優先)
        raise XOAuth2Error(
            f"{path} が壊れている({type(e).__name__})。削除して intel x-auth で再認可する"
        ) from e


def save_token(config_dir: Path, token: XOAuth2Token) -> None:
    """0600 の一時ファイル → rename の原子的更新(ローテーション取りこぼし防止)。"""
    path = token_path(config_dir)
    tmp = path.parent / (path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(token), f, ensure_ascii=False)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


# --- PKCE / 認可 URL ---


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)。verifier は RFC 7636 の 43〜128 字内。"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(OAUTH2_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --- token エンドポイント ---


def exchange_code(
    client: httpx.Client,
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> XOAuth2Token:
    return _post_token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        client_id,
        client_secret,
    )


def refresh_token_grant(
    client: httpx.Client, *, client_id: str, client_secret: str | None, refresh_token: str
) -> XOAuth2Token:
    return _post_token(
        client,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        client_id,
        client_secret,
    )


def _post_token(
    client: httpx.Client, data: dict[str, str], client_id: str, client_secret: str | None
) -> XOAuth2Token:
    headers = {}
    if client_secret:
        # confidential client(portal で Web App 型)は Basic 認証
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    else:
        # public client(Native App 型)は body の client_id
        data = {**data, "client_id": client_id}
    response = client.post(TOKEN_URL, data=data, headers=headers, timeout=TIMEOUT_S)
    if response.status_code != 200:
        # token エンドポイントのエラー本文は error/error_description のみ(秘匿情報なし)
        raise XOAuth2Error(
            f"token エンドポイント {response.status_code}: {response.text[:200]}"
        )
    body = response.json()
    if "refresh_token" not in body:
        raise XOAuth2Error(
            "応答に refresh_token がない(offline.access scope が付与されているか確認)"
        )
    expires_at = _to_iso(
        datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 7200)))
    )
    return XOAuth2Token(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=expires_at,
        scope=body.get("scope", " ".join(OAUTH2_SCOPES)),
    )


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- 認可フロー(intel x-auth) ---


class CallbackServer:
    """loopback redirect(http://127.0.0.1:<port>/callback)を1回だけ受けるサーバ。

    ブラウザは favicon 等の別リクエストも送るため、/callback を受けるまで
    deadline 内でリクエストを受け続ける。state 不一致は CSRF の可能性として中断する。
    """

    def __init__(self, port: int) -> None:
        outcome: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802(stdlib の契約名)
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                params = dict(parse_qsl(parsed.query))
                if "error" in params:
                    outcome["error"] = f"認可が拒否された: {params['error']}"
                elif "code" not in params or "state" not in params:
                    outcome["error"] = "callback に code/state がない"
                else:
                    # state の照合は wait_for_code 側(expected_state はここに無い)
                    outcome["code"] = params["code"]
                    outcome["state"] = params["state"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body>認可を受け付けました。ターミナルに戻ってください。"
                    "</body></html>".encode()
                )

            def log_message(self, *args: object) -> None:
                pass  # 標準 stderr を汚さない(進捗は CLI 側が出す)

        self._outcome = outcome
        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._server.timeout = 1.0  # 1リクエスト待ちの単位(deadline は wait_for_code)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def wait_for_code(self, expected_state: str, timeout_s: int) -> str:
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                self._server.handle_request()
                if "error" in self._outcome:
                    raise XOAuth2Error(self._outcome["error"])
                if "code" in self._outcome:
                    if not secrets.compare_digest(self._outcome["state"], expected_state):
                        raise XOAuth2Error("state 不一致(CSRF の可能性)。認可をやり直す")
                    return self._outcome["code"]
            raise XOAuth2Error(f"認可 callback を {timeout_s}s 以内に受信できなかった")
        finally:
            self._server.server_close()


def run_authorization_flow(
    client: httpx.Client,
    *,
    config_dir: Path,
    client_id: str,
    client_secret: str | None,
    callback_port: int,
    echo: Callable[[str], None],
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout_s: int = 300,
) -> XOAuth2Token:
    """PKCE 認可フローを実行し、取得した token を保存して返す。"""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    server = CallbackServer(callback_port)  # redirect_uri は portal 登録と完全一致が必要
    redirect_uri = f"http://127.0.0.1:{server.port}/callback"
    url = build_authorize_url(client_id, redirect_uri, state, challenge)
    echo("ブラウザで認可してください(開かない場合は URL を手動で開く):")
    echo(url)
    open_browser(url)
    code = server.wait_for_code(state, timeout_s)
    token = exchange_code(
        client,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    save_token(config_dir, token)
    return token


# --- 認証つきセッション(S1b の取込コネクタ・doctor #13 が使う) ---


class XOAuth2Session:
    """OAuth 2.0 Bearer つき GET(共通リトライ規約 = 設計 05 §8。XApiSession と同形)。

    - 失効 REFRESH_MARGIN_S 前と 401 で refresh し、ローテーションされた token を
      使用前に永続化する(保存失敗時に旧 token で続行しない)
    - refresh 失敗は再認可(intel x-auth)の案内つき XOAuth2Error
    - 429 はリトライせず即返す(rate limit は待たず打ち切る = 設計 02 §2 と同方針)
    """

    def __init__(
        self,
        *,
        client: httpx.Client,
        config_dir: Path,
        client_id: str,
        client_secret: str | None,
        token: XOAuth2Token,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._config_dir = config_dir
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = token
        self._sleep = sleep
        self._now = now

    @classmethod
    def load(
        cls,
        *,
        client: httpx.Client,
        config_dir: Path,
        client_id: str,
        client_secret: str | None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> XOAuth2Session:
        token = load_token(config_dir)
        if token is None:
            raise XOAuth2Error("OAuth2 token 未取得(intel x-auth で認可する)")
        return cls(
            client=client,
            config_dir=config_dir,
            client_id=client_id,
            client_secret=client_secret,
            token=token,
            sleep=sleep,
            now=now,
        )

    def get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        self._ensure_fresh()
        full_url = f"{url}?{urlencode(params)}" if params else url
        last_error: Exception | None = None
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token.access_token}"}
            try:
                response = self._client.get(full_url, headers=headers, timeout=TIMEOUT_S)
            except httpx.HTTPError as e:
                last_error = XOAuth2Error(f"X 接続失敗: {type(e).__name__}")
                self._backoff(attempt)
                continue
            if response.status_code == 401 and not refreshed:
                # margin 内でも失効しうる(時計ずれ・失効前倒し)。1回だけ refresh して再試行
                self._refresh()
                refreshed = True
                continue
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                self._backoff(attempt)
                continue
            return response
        raise last_error  # transport error のリトライ尽き(直前 continue で必ず設定済み)

    def _ensure_fresh(self) -> None:
        expires_at = _from_iso(self._token.expires_at)
        if self._now() >= expires_at - timedelta(seconds=REFRESH_MARGIN_S):
            self._refresh()

    def _refresh(self) -> None:
        try:
            new_token = refresh_token_grant(
                self._client,
                client_id=self._client_id,
                client_secret=self._client_secret,
                refresh_token=self._token.refresh_token,
            )
        except XOAuth2Error as e:
            raise XOAuth2Error(f"refresh 失敗(intel x-auth で再認可が必要): {e}") from e
        save_token(self._config_dir, new_token)  # 先に永続化(取りこぼすと認可が切れる)
        self._token = new_token

    def _backoff(self, attempt: int) -> None:
        if attempt < MAX_RETRIES:
            self._sleep(BACKOFF_S[attempt] + random.random())


def fetch_me(session: XOAuth2Session) -> str:
    """/2/users/me で認証を検証し username を返す(読取課金なし。doctor #13 / x-auth 用)。"""
    response = session.get(USERS_ME_URL)
    if response.status_code != 200:
        raise XOAuth2Error(f"users/me {response.status_code}: {response.text[:200]}")
    data = response.json().get("data", {})
    username = data.get("username")
    if not username:
        raise XOAuth2Error("users/me の応答に username がない")
    return username
