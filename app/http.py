"""SSRF ガード付き共通 HTTP クライアント(設計 05 §8)。

外向き HTTP の全経路(RSS 取得・本文抽出・短縮 URL 展開)はここを通す:
- timeout: connect 10s / read 30s
- リトライ: 2回・指数バックオフ(1s→4s)+ jitter。429 は Retry-After 尊重。
  恒久 4xx はリトライしない(ステータスの業務分類は呼び出し側)
- SSRF ガード: DNS 解決後の接続先 IP が private / loopback / link-local 帯なら拒否。
  リダイレクトは自動追跡せず各ホップで再検査する
"""

from __future__ import annotations

import ipaddress
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.connectors.base import FatalError, RetryableError

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
MAX_RETRIES = 2  # 初回 + リトライ2回
BACKOFF_S = (1.0, 4.0)
MAX_REDIRECTS = 5
USER_AGENT = "intel/0.1 (personal digest bot)"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PERMANENT_REDIRECTS = frozenset({301, 308})


class SsrfBlockedError(FatalError):
    """接続先が private / loopback / link-local 帯(設計 05 §8)。"""


@dataclass(frozen=True)
class FetchedResponse:
    response: httpx.Response
    final_url: str  # リダイレクト追跡後の URL
    permanently_moved: bool  # 301/308 を経由した(RSS の警告表示用。設計 02 §3)


def _default_resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


class HttpClient:
    """テストは transport(httpx.MockTransport)と resolve_host を注入する。"""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve_host: Callable[[str], list[str]] = _default_resolve_host,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._resolve_host = resolve_host
        self._sleep = sleep
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,  # 各ホップで SSRF 再検査するため手動追跡
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
        リトライ対象は transport エラー・5xx・429 のみ)。
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                self._sleep(BACKOFF_S[attempt - 1] + random.random())
            try:
                fetched = self._follow_redirects(url, headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_error = e
                continue
            status = fetched.response.status_code
            if status == 429:
                last_error = RetryableError(f"HTTP 429: {url}")
                retry_after = fetched.response.headers.get("retry-after")
                if retry_after is not None and retry_after.isdigit():
                    self._sleep(int(retry_after))
                continue
            if 500 <= status <= 599:
                last_error = RetryableError(f"HTTP {status}: {url}")
                continue
            return fetched
        raise RetryableError(f"リトライ上限({MAX_RETRIES}回)到達: {url}") from last_error

    def resolve_short_url(self, url: str) -> str:
        """既知短縮 URL の最終 URL 解決(u1 手順1 の resolver。設計 01 §4)。"""
        return self.get(url).final_url

    def _follow_redirects(self, url: str, headers: dict[str, str] | None) -> FetchedResponse:
        current = url
        permanently_moved = False
        for _ in range(MAX_REDIRECTS + 1):
            self._check_target(current)
            response = self._client.get(current, headers=headers)
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

    def _check_target(self, url: str) -> None:
        host = urlsplit(url).hostname
        if not host:
            raise FatalError(f"ホストのない URL: {url[:80]}")
        try:
            addresses = self._resolve_host(host)
        except OSError as e:
            raise RetryableError(f"DNS 解決失敗: {host}") from e
        if not addresses:
            raise RetryableError(f"DNS 解決結果が空: {host}")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise SsrfBlockedError(f"内部アドレスへの接続を拒否: {host} -> {address}")
