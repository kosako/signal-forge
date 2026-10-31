"""X OAuth 2.0(PKCE)認証基盤(S1 = GH#58 / ADR 0001「X ソースと認証」)。

ブックマーク取得(/2/users/:id/bookmarks)は OAuth 2.0 user context が API 上の必須で、
S1 の取込(ブックマーク・いいね)は認証経路を1本に保つため OAuth2 に統一する(ADR 0001)。

前提事実(API 実確認 2026-07-13):
- access token は offline.access を付けても 2h で失効する。無人朝バッチには
  refresh token(offline.access scope)が必須
- refresh token はローテーションされる(使用のたびに新 refresh token が発行される)
  前提で設計する。新 token の永続化を取りこぼすと認可チェーンが切れ、再認可
  (intel x-auth)が必要になるため、保存を先に行い原子的に書く。プロセス間の
  同時 refresh(朝バッチ + doctor)も同じ理由でファイルロックで直列化する

token ファイルは config_dir 直下・chmod 600(.env と同じ扱い。設計 05 §6)。
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import random
import secrets
import stat as stat_module
import tempfile
import time
import webbrowser
from collections.abc import Callable, Iterator
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
LOCK_FILENAME = "x-oauth2-token.lock"
ENV_CLIENT_SECRET = "X_OAUTH2_CLIENT_SECRET"  # confidential client のみ(.env・任意)
REFRESH_MARGIN_S = 300  # 失効のこの秒数前から先回りで refresh(バッチ途中の失効を避ける)
HTTP_TIMEOUT = httpx.Timeout(10.0, read=30.0)  # connect 10s / read 30s(設計 05 §8)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    """保存済み token を返す。未認可(ファイル無し)は None。壊れたファイルは fail fast。

    .env と同じ規律(設計 05 §6): group/other に開いたファイルは読まない。
    symlink は拒否する(すり替えで別ファイルを秘匿情報として読まされない)。
    """
    path = token_path(config_dir)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise XOAuth2Error(
            f"{path} を開けない({type(e).__name__})。symlink なら削除して intel x-auth"
        ) from e
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            raise XOAuth2Error(f"{path} が通常ファイルでない。削除して intel x-auth で再認可する")
        if info.st_mode & 0o077:
            raise XOAuth2Error(
                f"{path}: パーミッションが {info.st_mode & 0o777:o}。"
                "group/other に開いた token は読み込まない(chmod 600 にすること)"
            )
        raw = os.read(fd, info.st_size)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
        token = XOAuth2Token(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
            scope=data["scope"],
        )
        # 意味的破損(null・空文字・parse 不能/naive な期限・scope 欠け)も load 境界で
        # fail fast する(naive 日時は失効判定の aware 比較で TypeError になる)
        if not all(
            isinstance(v, str) and v
            for v in (token.access_token, token.refresh_token, token.expires_at, token.scope)
        ):
            raise ValueError("空または非文字列のフィールド")
        if _from_iso(token.expires_at).tzinfo is None:
            raise ValueError("expires_at に timezone がない")
        if set(OAUTH2_SCOPES) - set(token.scope.split()):
            raise ValueError("必要な scope が欠けている")
        return token
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        # 黙って再認可に倒さない(半端な上書き・手編集ミスの検出を優先)
        raise XOAuth2Error(
            f"{path} が壊れている({type(e).__name__})。削除して intel x-auth で再認可する"
        ) from e


def save_token(config_dir: Path, token: XOAuth2Token) -> None:
    """0600 の一時ファイル → rename の原子的更新(ローテーション取りこぼし防止)。

    一時ファイルは mkstemp(O_EXCL・ランダム名・0600)で作る(固定名だと既存の
    symlink を辿って書かされうる)。最終パスへの rename は symlink 自体を置換する。
    """
    path = token_path(config_dir)
    fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=".x-oauth2-token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(token), f, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _token_lock(config_dir: Path) -> Iterator[None]:
    """refresh のプロセス間直列化(朝バッチ + doctor の同時 refresh でローテーション競合
    = 片方が invalid_grant で認可チェーンを失うのを防ぐ)。再認可(intel x-auth)の
    交換・保存も同じロックに入る(refresh の保存で再認可の token を上書きさせない。GH#233)。"""
    lock_path = config_dir / LOCK_FILENAME
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # close で flock は解放される


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
    now: Callable[[], datetime] = _utcnow,
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
        # 初回交換で scope が省略された応答は「要求どおり付与」とみなす
        fallback_scope=" ".join(OAUTH2_SCOPES),
        now=now,
    )


def refresh_token_grant(
    client: httpx.Client,
    *,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    fallback_scope: str,
    now: Callable[[], datetime] = _utcnow,
) -> XOAuth2Token:
    return _post_token(
        client,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        client_id,
        client_secret,
        fallback_scope=fallback_scope,  # refresh 応答の scope 省略時は検証済みの値を維持
        now=now,
    )


def _post_token(
    client: httpx.Client,
    data: dict[str, str],
    client_id: str,
    client_secret: str | None,
    *,
    fallback_scope: str,
    now: Callable[[], datetime],
) -> XOAuth2Token:
    headers = {}
    if client_secret:
        # confidential client(portal で Web App 型)は Basic 認証
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("ascii")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    else:
        # public client(Native App 型)は body の client_id
        data = {**data, "client_id": client_id}
    try:
        response = client.post(TOKEN_URL, data=data, headers=headers, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        # 通信の失敗も分類済みの例外にする(GET 側の _request_with_retry と同じ。素の httpx の
        # 例外は取込の kind 単位の隔離を抜け、台帳の記録を飛ばす。GH#330)。要求は refresh token
        # と client の資格情報を持つので、元の例外はつながず型の名前だけを残す
        raise XOAuth2Error(f"token エンドポイントに接続できない: {type(e).__name__}") from None
    if response.status_code != 200:
        raise XOAuth2Error(
            f"token エンドポイント {response.status_code}: {_safe_error_detail(response)}"
        )
    body = response.json()
    if "refresh_token" not in body:
        raise XOAuth2Error(
            "応答に refresh_token がない(offline.access scope が付与されているか確認)"
        )
    scope = body.get("scope") or fallback_scope
    missing = set(OAUTH2_SCOPES) - set(scope.split())
    if missing:
        # scope 縮小を「認可完了」として保存しない(S1b の取込だけが後で失敗する事故防止)
        raise XOAuth2Error(
            f"必要な scope が付与されていない: {' '.join(sorted(missing))}"
            "(portal の App permissions と認可画面を確認)"
        )
    expires_at = _to_iso(now() + timedelta(seconds=int(body.get("expires_in", 7200))))
    return XOAuth2Token(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=expires_at,
        scope=scope,
    )


# RFC 6749 §5.2 + X が返す既知の error code。これ以外の値は表示しない
_KNOWN_OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_response_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
    }
)


def _safe_error_detail(response: httpx.Response) -> str:
    """既知の OAuth error code に一致したときだけ表示する(allowlist)。

    エラー本文は gateway/API が要求内容(token・code)を任意フィールドに反射しうる
    untrusted データ。値の切り詰めでは不十分で、一致以外は固定文言に落とす。
    """
    try:
        body = response.json()
    except ValueError:
        return "(本文非表示)"
    if isinstance(body, dict):
        code = body.get("error")
        if isinstance(code, str) and code in _KNOWN_OAUTH_ERRORS:
            return code
    return "(本文非表示)"


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- 認可フロー(intel x-auth) ---


class CallbackServer:
    """loopback redirect(http://127.0.0.1:<port>/callback)を1回だけ受けるサーバ。

    - ブラウザは favicon 等の別リクエストも送るため、/callback を受けるまで
      deadline 内でリクエストを受け続ける
    - state は成功(code)・拒否(error)の両経路で照合する。不一致は 400 を返して
      無視し待ち続ける(偽 callback でフローを中断・確定させられないようにする)
    """

    def __init__(self, port: int, expected_state: str) -> None:
        outcome: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802(stdlib の契約名)
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_error(404)
                    return
                params = dict(parse_qsl(parsed.query))
                state = params.get("state", "")
                if not secrets.compare_digest(state, expected_state):
                    self.send_error(400)  # 偽 callback は無視(照合失敗を外に教えない)
                    return
                if "error" in params:
                    outcome["error"] = f"認可が拒否された: {params['error'][:50]}"
                elif "code" in params:
                    outcome["code"] = params["code"]
                else:
                    outcome["error"] = "callback に code がない"
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

    def wait_for_code(self, timeout_s: int) -> str:
        try:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                self._server.handle_request()
                if "error" in self._outcome:
                    raise XOAuth2Error(self._outcome["error"])
                if "code" in self._outcome:
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
    # redirect_uri は portal 登録と完全一致が必要(runbook §9)
    server = CallbackServer(callback_port, state)
    redirect_uri = f"http://127.0.0.1:{server.port}/callback"
    url = build_authorize_url(client_id, redirect_uri, state, challenge)
    echo("ブラウザで認可してください(開かない場合は URL を手動で開く):")
    echo(url)
    open_browser(url)
    code = server.wait_for_code(timeout_s)
    # 交換から保存までを refresh と同じロックで直列化する(GH#233)。朝バッチ・doctor の
    # refresh の往復中に再認可すると、後から保存される refresh の結果が再認可の token を
    # 上書きしうるため。ブラウザ待ちの間はロックを持たない(その間の refresh を止めない)
    with _token_lock(config_dir):
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
      使用前に永続化する。refresh はプロセス間ロックで直列化し、他プロセスが先に
      refresh していた場合はディスクの新 token を採用する(二重 refresh で
      invalid_grant にしない)
    - 401 は transport/5xx のリトライ予算とは別に、refresh 後の再送を必ず1回行う
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
        now: Callable[[], datetime] = _utcnow,
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
        now: Callable[[], datetime] = _utcnow,
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
        if self._is_expiring(self._token):
            self._refresh()
        full_url = f"{url}?{urlencode(params)}" if params else url
        response = self._request_with_retry(full_url)
        if response.status_code == 401:
            # margin 内でも失効しうる(時計ずれ・失効前倒し・revoke)。refresh 後に
            # 必ず1回再送する(リトライ予算のどの試行で 401 が来ても取りこぼさない)
            self._refresh()
            response = self._request_with_retry(full_url)
        return response  # refresh 後の 401 はそのまま返す(呼び出し側で恒久エラー扱い)

    def _request_with_retry(self, full_url: str) -> httpx.Response:
        """transport/5xx のリトライ(設計 05 §8)。401/429/その他はそのまま返す。"""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token.access_token}"}
            try:
                response = self._client.get(full_url, headers=headers, timeout=HTTP_TIMEOUT)
            except httpx.HTTPError as e:
                last_error = XOAuth2Error(f"X 接続失敗: {type(e).__name__}")
                self._backoff(attempt)
                continue
            if response.status_code >= 500 and attempt < MAX_RETRIES:
                self._backoff(attempt)
                continue
            return response
        raise last_error  # transport error のリトライ尽き(直前 continue で必ず設定済み)

    def _is_expiring(self, token: XOAuth2Token) -> bool:
        return self._now() >= _from_iso(token.expires_at) - timedelta(
            seconds=REFRESH_MARGIN_S
        )

    def _refresh(self) -> None:
        with _token_lock(self._config_dir):
            # 他プロセス(朝バッチ ⟷ doctor)が先に refresh していたらその token を採用
            # (旧 refresh token は使用済み。再送すると invalid_grant で認可を失う)。
            # 採用した token も失効間近なら、新しい refresh token で refresh を続行する
            on_disk = load_token(self._config_dir)
            if on_disk is not None and on_disk.refresh_token != self._token.refresh_token:
                self._token = on_disk
                if not self._is_expiring(on_disk):
                    return
            try:
                new_token = refresh_token_grant(
                    self._client,
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                    refresh_token=self._token.refresh_token,
                    fallback_scope=self._token.scope,
                    now=self._now,
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
        raise XOAuth2Error(
            f"users/me {response.status_code}: {_safe_error_detail(response)}"
        )
    data = response.json().get("data", {})
    username = data.get("username")
    if not username:
        raise XOAuth2Error("users/me の応答に username がない")
    return username
