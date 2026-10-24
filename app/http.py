"""SSRF ガード付き共通 HTTP クライアント(設計 05 §8)。

外向き HTTP の全経路(RSS 取得・本文抽出・短縮 URL 展開)はここを通す:
- timeout: connect 10s / read 30s。加えて get 1 回の全体(リダイレクト・リトライ・待機を
  含む)に期限 60s を付ける(GH#203)
- リトライ: 2回・指数バックオフ(1s→4s)+ jitter。429 は Retry-After 尊重(30s まで。より長い
  待ちを求められたら待たずに失敗)。恒久 4xx はリトライしない(ステータスの業務分類は呼び出し側)
- 本文はストリーミングで受け、展開後 10 MiB を超えたら読むのをやめて失敗にする(GH#203)
- SSRF ガード: DNS 解決後の接続先 IP が global でなければ拒否(is_global を基本にした
  allowlist 方式。private/loopback/link-local に加え shared address space 100.64.0.0/10 等
  のブラックリスト漏れも遮断)。リダイレクトは自動追跡せず各ホップで再検査する。
  接続は**検証済み IP にピン留め**する(URL ホストを IP に書き換え、Host ヘッダ+SNI で
  元ホスト名を維持)。検証と接続で DNS を別々に引くと、attacker 制御の DNS が
  1回目 public・2回目 private を返す rebinding で検査を回避できるため
"""

from __future__ import annotations

import ipaddress
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.connectors.base import FatalError, RetryableError

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
MAX_RETRIES = 2  # 初回 + リトライ2回
BACKOFF_S = (1.0, 4.0)
MAX_REDIRECTS = 5
USER_AGENT = "intel/0.1 (personal digest bot)"
# 接続先の応答で収集の資源を占有されないための上限(GH#203)。RSS・本文の HTML とも普通は
# 数 MB に収まるので、展開後の本文は 10 MiB まで。1 回の get の全体の期限は、read timeout の
# 2 回分とバックオフが収まる 60s。Retry-After は期限の中で 1 回待てる 30s まで
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
FETCH_DEADLINE_S = 60
MAX_RETRY_AFTER_S = 30

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PERMANENT_REDIRECTS = frozenset({301, 308})


class SsrfBlockedError(FatalError):
    """接続先が global でないアドレス(is_global allowlist 方式。設計 05 §8)。"""


class DnsResolutionError(RetryableError):
    """名前解決の失敗(解決できない・結果が空)。一時的な失敗として再試行する(GH#188)。"""


class ResponseTooLargeError(FatalError):
    """応答の本文(展開後)が上限を超えた。読み直しても同じなので再試行しない(GH#203)。"""


class FetchDeadlineError(RetryableError):
    """リダイレクト・リトライ・待機を含む取得の全体の期限を超えた。再試行しない(GH#203)。"""


@dataclass(frozen=True)
class FetchedResponse:
    response: httpx.Response
    final_url: str  # リダイレクト追跡後の URL
    permanently_moved: bool  # 301/308 を経由した(RSS の警告表示用。設計 02 §3)


def _default_resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _failure_detail(last_error: Exception | None) -> str:
    # DNS で尽きたときは失敗したホストを文言に残す。呼び出し側は例外の型と文字列だけを
    # 記録し、リダイレクト先のホストは元 URL からは分からないため(GH#188)
    return f"({last_error})" if isinstance(last_error, DnsResolutionError) else ""


class HttpClient:
    """テストは transport(httpx.MockTransport)と resolve_host を注入する。"""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve_host: Callable[[str], list[str]] = _default_resolve_host,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolve_host = resolve_host
        self._sleep = sleep
        self._clock = clock  # 取得の全体の期限の計測用(GH#203)
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,  # 各ホップで SSRF 再検査するため手動追跡
            trust_env=False,  # HTTP(S)_PROXY 経由だと検査済み IP と実接続先がずれるため無視
            # IP ピン留めでは接続プールのキーが IP origin になり、同一 IP に pin された
            # 別ホストへ前ホスト向け TLS 接続が再利用されうる(SNI/証明書検証は接続
            # 作成時のみ)。keep-alive を切り、リクエストごとに TLS を張り直す
            limits=httpx.Limits(max_keepalive_connections=0),
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                read=READ_TIMEOUT_S,
                write=READ_TIMEOUT_S,
                pool=CONNECT_TIMEOUT_S,
            ),
            headers={"User-Agent": USER_AGENT},
        )

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchedResponse:
        """リトライ+リダイレクト追跡付き GET。

        2xx/3xx 以外もレスポンスとして返す(304 は正常系、4xx の業務分類は呼び出し側。
        リトライ対象は transport エラー・DNS 解決失敗・5xx・429 のみ)。
        本文の上限(ResponseTooLargeError)と全体の期限(FetchDeadlineError)の超過は
        リトライせずに送出する(GH#203)。
        """
        deadline = self._clock() + FETCH_DEADLINE_S
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                fetched = self._follow_redirects(url, headers, deadline)
            except (httpx.TransportError, httpx.TimeoutException, DnsResolutionError) as e:
                # スリープ復帰直後などの一時的な名前解決の失敗も、transport エラーと
                # 同じ回数・バックオフで再試行する(GH#188)
                last_error = e
                self._backoff(attempt, url, deadline, last_error)
                continue
            status = fetched.response.status_code
            if status == 429:
                last_error = RetryableError(f"HTTP 429: {url}")
                # Retry-After 優先(追加バックオフは重ねない)。最終試行後は待たない
                retry_after = fetched.response.headers.get("retry-after")
                if attempt < MAX_RETRIES:
                    if retry_after is not None and retry_after.isdigit():
                        if int(retry_after) > MAX_RETRY_AFTER_S:
                            # 求められた間隔より早く試し直しても断られるだけなので、待たずに
                            # このソース・記事の失敗にする(GH#203)
                            raise RetryableError(
                                f"HTTP 429 の Retry-After({retry_after}s)が上限"
                                f"({MAX_RETRY_AFTER_S}s)を超える: {url}"
                            )
                        self._wait(int(retry_after), url, deadline, last_error)
                    else:
                        self._backoff(attempt, url, deadline, last_error)
                continue
            if 500 <= status <= 599:
                last_error = RetryableError(f"HTTP {status}: {url}")
                self._backoff(attempt, url, deadline, last_error)
                continue
            return fetched
        raise RetryableError(
            f"リトライ上限({MAX_RETRIES}回)到達: {url}{_failure_detail(last_error)}"
        ) from last_error

    def _backoff(self, attempt: int, url: str, deadline: float, last_error: Exception) -> None:
        if attempt < MAX_RETRIES:
            self._wait(BACKOFF_S[attempt] + random.random(), url, deadline, last_error)

    def _wait(self, seconds: float, url: str, deadline: float, last_error: Exception) -> None:
        """リトライ前に待つ。待つと期限までに試す時間が残らないなら待たずに打ち切る(GH#203)。"""
        if seconds >= deadline - self._clock():
            raise FetchDeadlineError(
                f"取得の期限({FETCH_DEADLINE_S}s)内にリトライできない: "
                f"{url}{_failure_detail(last_error)}"
            ) from last_error
        self._sleep(seconds)

    def _remaining(self, deadline: float, url: str) -> float:
        """取得の期限までの残り秒数。過ぎていれば FetchDeadlineError(GH#203)。"""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise FetchDeadlineError(f"取得の期限({FETCH_DEADLINE_S}s)を超過: {url}")
        return remaining

    def resolve_short_url(self, url: str) -> str:
        """既知短縮 URL の最終 URL 解決(u1 手順1 の resolver。設計 01 §4)。"""
        return self.get(url).final_url

    def _follow_redirects(
        self, url: str, headers: dict[str, str] | None, deadline: float
    ) -> FetchedResponse:
        current = url
        permanently_moved = False
        for _ in range(MAX_REDIRECTS + 1):
            self._remaining(deadline, current)  # 期限を過ぎたら次のホップへ進まない
            pinned_ip = self._check_target(current)
            response = self._get_pinned(current, pinned_ip, headers, deadline)
            if response.status_code not in _REDIRECT_STATUSES:
                return FetchedResponse(
                    response=response, final_url=current, permanently_moved=permanently_moved
                )
            location = response.headers.get("location")
            if not location:
                raise FatalError(f"Location ヘッダのないリダイレクト: {current}")
            permanently_moved = permanently_moved or (
                response.status_code in _PERMANENT_REDIRECTS
            )
            current = urljoin(current, location)
        raise FatalError(f"リダイレクトが {MAX_REDIRECTS} 回を超過(ループ疑い): {url}")

    def _get_pinned(
        self, url: str, ip: str, headers: dict[str, str] | None, deadline: float
    ) -> httpx.Response:
        """URL ホストを検証済み IP に書き換えて GET する(DNS rebinding 対策)。

        Host ヘッダと SNI(sni_hostname)に元ホスト名を残すため、サーバ側からは
        通常のリクエストに見える。証明書検証も元ホスト名に対して行われる。
        本文はストリーミングで受け、受信量と期限を検査しながら読む(GH#203)。
        """
        split = urlsplit(url)
        host = split.hostname
        if host is None:  # _check_target 通過済みだが契約として明示
            raise FatalError(f"ホストのない URL: {url[:80]}")
        ip_literal = f"[{ip}]" if ":" in ip else ip  # IPv6 はブラケット表記
        userinfo = ""
        if split.username is not None:
            userinfo = split.username
            if split.password is not None:
                userinfo += f":{split.password}"
            userinfo += "@"
        try:
            port = split.port
        except ValueError as e:
            raise FatalError(f"ポートが不正な URL: {url[:80]}") from e
        port_suffix = f":{port}" if port is not None else ""
        pinned_url = urlunsplit(
            (split.scheme, f"{userinfo}{ip_literal}{port_suffix}", split.path, split.query, "")
        )
        host_header = f"{host}{port_suffix}"
        request_headers = {**(headers or {}), "Host": host_header}
        extensions = {"sni_hostname": host} if split.scheme.lower() == "https" else {}
        # 1 回の接続・受信の待ちも期限の残りで縮め、期限を大きく越えて待たない
        remaining = self._remaining(deadline, url)
        timeout = httpx.Timeout(
            connect=min(CONNECT_TIMEOUT_S, remaining),
            read=min(READ_TIMEOUT_S, remaining),
            write=min(READ_TIMEOUT_S, remaining),
            pool=min(CONNECT_TIMEOUT_S, remaining),
        )
        try:
            with self._client.stream(
                "GET", pinned_url, headers=request_headers, extensions=extensions, timeout=timeout
            ) as response:
                self._read_body(response, url, deadline)
            return response
        finally:
            # cookie jar は IP origin に紐づくため、同一 IP に pin された別ホストへ
            # cookie が流れる。クライアントは cookie を契約に持たない(RSS/本文取得のみ)
            # ので毎リクエスト破棄する
            self._client.cookies.clear()

    def _read_body(self, response: httpx.Response, url: str, deadline: float) -> None:
        """本文を上限と期限を検査しながら読み、response.content で読めるようにする(GH#203)。

        上限は content-encoding(gzip 等)を展開した後のバイト数で数える(小さく圧縮した
        巨大な本文も止める)。少しずつ送り続ける応答は read timeout では切れないため、
        受信のたびに期限も見る。
        """
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"応答の本文が上限({MAX_RESPONSE_BYTES} バイト)を超える: {url}"
                )
            chunks.append(chunk)
            self._remaining(deadline, url)
        # httpx に上限付きで読む公開の口が無いため、read() と同じく _content に入れて
        # 通常の取得と同じに content / text で読めるようにする(httpx の版は uv.lock で固定)
        response._content = b"".join(chunks)

    def _check_target(self, url: str) -> str:
        """接続先を検証し、接続に使う検証済み IP を返す。"""
        host = urlsplit(url).hostname
        if not host:
            raise FatalError(f"ホストのない URL: {url[:80]}")
        try:
            addresses = self._resolve_host(host)
        except OSError as e:
            raise DnsResolutionError(f"DNS 解決失敗: {host}") from e
        if not addresses:
            raise DnsResolutionError(f"DNS 解決結果が空: {host}")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # ブラックリスト方式は shared address space(100.64.0.0/10)等が素通りするため、
            # is_global を基本にした allowlist 方式で判定する
            if not ip.is_global or ip.is_multicast:
                raise SsrfBlockedError(
                    f"グローバルでないアドレスへの接続を拒否: {host} -> {address}"
                )
        return addresses[0]
