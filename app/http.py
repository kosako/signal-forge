"""SSRF ガード付き共通 HTTP クライアント(設計 05 §8)。

外向き HTTP の全経路(RSS 取得・本文抽出・短縮 URL 展開)はここを通す:
- timeout: connect 10s / read 30s。加えて get 1 回の全体(名前解決・接続・受信・リダイレクト・
  リトライ・待機を含む)に期限 60s を付ける(GH#203)
- リトライ: 2回・指数バックオフ(1s→4s)+ jitter。429 は Retry-After 尊重(30s まで。より長い
  待ちを求められたら待たずに失敗)。恒久 4xx はリトライしない(ステータスの業務分類は呼び出し側)
- 本文はストリーミングで受け、受信量(展開前)か展開後が 10 MiB を超えたら読むのをやめて失敗に
  する。展開は少しずつ行い、上限の検査の前に巨大に展開しない(GH#203)。短縮 URL の展開は
  転送を各ホップのヘッダで判定し、本文は読まない(GH#328)
- SSRF ガード: DNS 解決後の接続先 IP が global でなければ拒否(is_global を基本にした
  allowlist 方式。private/loopback/link-local に加え shared address space 100.64.0.0/10 等
  のブラックリスト漏れも遮断)。リダイレクトは自動追跡せず各ホップで再検査する。
  接続は**検証済み IP にピン留め**する(URL ホストを IP に書き換え、Host ヘッダ+SNI で
  元ホスト名を維持)。検証と接続で DNS を別々に引くと、attacker 制御の DNS が
  1回目 public・2回目 private を返す rebinding で検査を回避できるため
"""

from __future__ import annotations

import contextlib
import ipaddress
import random
import socket
import threading
import time
from collections.abc import Callable, Iterator
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
# 展開器へ渡す受信データの 1 回分。gzip / deflate の展開比は最大でも約 1032 倍なので、上限の
# 検査の前に 1 回で展開される量は 1 MiB 程度に収まる(GH#203)
_RAW_SLICE_BYTES = 1024
# 展開比を抑えられる符号化だけを求める。httpx の既定は br / zstd の展開器を入れると求める
# 符号化に加えるため、明示して固定する(GH#203)
_ACCEPT_ENCODING = "gzip, deflate"
_EXPANDING_CODINGS = frozenset({"gzip", "deflate", "br", "zstd"})  # httpx が展開しうる符号化
# 期限で見捨てた名前解決のスレッドは止められないので、同時に走る数に上限を付ける。収集と本文
# 抽出は直列に取得するので、ふだん走るのは 1 本。埋まっていれば新しく作らずに失敗にする(GH#203)
_MAX_RESOLVER_THREADS = 4
_resolver_slots = threading.BoundedSemaphore(_MAX_RESOLVER_THREADS)

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


class _SlicedStream(httpx.SyncByteStream):
    """受信したデータを小さく分けて展開器へ渡し、受信量(展開前)も上限で止める(GH#203)。

    httpx は受信の 1 回分(最大 64 KiB)を出力の上限なしで展開するため、そのままでは小さく
    圧縮した巨大な本文が上限の検査の前にメモリを使い切りうる。また展開器は圧縮データの終わりの
    後ろに届いたデータを、出力なしで内部にためる。展開後のバイト数だけでは止められないので、
    展開器へ渡す前の受信量も数える。
    """

    def __init__(self, inner: httpx.SyncByteStream, url: str) -> None:
        self._inner = inner
        self._url = url

    def __iter__(self) -> Iterator[bytes]:
        received = 0
        for data in self._inner:
            received += len(data)
            if received > MAX_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    f"応答の受信量(展開前)が上限({MAX_RESPONSE_BYTES} バイト)を超える: {self._url}"
                )
            for start in range(0, len(data), _RAW_SLICE_BYTES):
                yield data[start : start + _RAW_SLICE_BYTES]

    def close(self) -> None:
        self._inner.close()


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):  # すでに閉じた接続
        sock.shutdown(socket.SHUT_RDWR)


class _DeadlineGuard:
    """期限が来たら接続を shutdown し、止まっている受信を終わらせる(GH#203)。

    read timeout は受信のたびに数え直すので、ヘッダや本文を少しずつ送り続ける相手は timeout
    では切れない。httpcore の trace 拡張で接続した socket を受け取って複製しておき、期限に
    shutdown する(TLS に包むと元の socket オブジェクトは切り離されるが、複製は同じ接続を指す)。
    """

    def __init__(self, seconds: float) -> None:
        self.expired = False
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def trace(self, event_name: str, info: dict) -> None:
        if event_name != "connection.connect_tcp.complete":
            return
        sock = info["return_value"].get_extra_info("socket")
        if sock is None:
            return
        with self._lock:
            duplicate = sock.dup()
            self._sockets.append(duplicate)
            if self.expired:
                _shutdown(duplicate)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            for sock in self._sockets:
                _shutdown(sock)

    def close(self) -> None:
        self._timer.cancel()
        with self._lock:
            for sock in self._sockets:
                sock.close()
            self._sockets.clear()


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
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": _ACCEPT_ENCODING},
        )

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchedResponse:
        """リトライ+リダイレクト追跡付き GET。

        2xx/3xx 以外もレスポンスとして返す(304 は正常系、4xx の業務分類は呼び出し側。
        リトライ対象は transport エラー・DNS 解決失敗・5xx・429 のみ)。
        本文の上限(ResponseTooLargeError)と全体の期限(FetchDeadlineError)の超過は
        リトライせずに送出する(GH#203)。
        """
        return self._fetch(url, headers, read_body=True)

    def resolve_short_url(self, url: str) -> str:
        """既知短縮 URL の最終 URL 解決(u1 手順1 の resolver。設計 01 §4)。

        転送の判定は各ホップの応答のヘッダだけで行い、本文は読まない(最終の URL しか使わない)。
        転送先が本文の上限を超える PDF などでも、URL は解決できる。各ホップの接続先の検証・
        リトライ・全体の期限は get と同じ(GH#328)。
        """
        return self._fetch(url, None, read_body=False).final_url

    def _fetch(
        self, url: str, headers: dict[str, str] | None, *, read_body: bool
    ) -> FetchedResponse:
        """get の本体。read_body が偽なら本文を読まずに接続を閉じる(content は読めない)。"""
        deadline = self._clock() + FETCH_DEADLINE_S
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                fetched = self._follow_redirects(url, headers, deadline, read_body=read_body)
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

    def _follow_redirects(
        self, url: str, headers: dict[str, str] | None, deadline: float, *, read_body: bool
    ) -> FetchedResponse:
        current = url
        permanently_moved = False
        for _ in range(MAX_REDIRECTS + 1):
            pinned_ip = self._check_target(current, deadline)  # 期限を過ぎていれば先へ進まない
            response = self._get_pinned(current, pinned_ip, headers, deadline, read_body=read_body)
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
        self,
        url: str,
        ip: str,
        headers: dict[str, str] | None,
        deadline: float,
        *,
        read_body: bool,
    ) -> httpx.Response:
        """URL ホストを検証済み IP に書き換えて GET する(DNS rebinding 対策)。

        Host ヘッダと SNI(sni_hostname)に元ホスト名を残すため、サーバ側からは
        通常のリクエストに見える。証明書検証も元ホスト名に対して行われる。
        本文はストリーミングで受け、受信量と期限を検査しながら読む(GH#203)。
        read_body が偽なら本文を読まずに接続を閉じる(短縮 URL の解決。GH#328)。
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
        # 1 回の接続・受信の待ちも期限の残りで縮める。それでも少しずつ送り続ける相手は
        # 切れないので、期限に接続を切る(GH#203)
        remaining = self._remaining(deadline, url)
        timeout = httpx.Timeout(
            connect=min(CONNECT_TIMEOUT_S, remaining),
            read=min(READ_TIMEOUT_S, remaining),
            write=min(READ_TIMEOUT_S, remaining),
            pool=min(CONNECT_TIMEOUT_S, remaining),
        )
        guard = _DeadlineGuard(remaining)
        extensions = {"trace": guard.trace}
        if split.scheme.lower() == "https":
            extensions["sni_hostname"] = host
        try:
            with self._client.stream(
                "GET", pinned_url, headers=request_headers, extensions=extensions, timeout=timeout
            ) as response:
                if read_body:
                    self._read_body(response, url, deadline)
        except httpx.TransportError as e:
            if guard.expired:
                raise FetchDeadlineError(f"取得の期限({FETCH_DEADLINE_S}s)を超過: {url}") from e
            raise
        finally:
            guard.close()
            # cookie jar は IP origin に紐づくため、同一 IP に pin された別ホストへ
            # cookie が流れる。クライアントは cookie を契約に持たない(RSS/本文取得のみ)
            # ので毎リクエスト破棄する
            self._client.cookies.clear()
        if guard.expired:
            # 期限で切った接続の終わりを、接続を閉じて終わる応答の本文の終わりと取り違えない
            raise FetchDeadlineError(f"取得の期限({FETCH_DEADLINE_S}s)を超過: {url}")
        return response

    def _read_body(self, response: httpx.Response, url: str, deadline: float) -> None:
        """本文を上限と期限を検査しながら読み、response.content で読めるようにする(GH#203)。

        上限は content-encoding(gzip 等)を展開した後のバイト数で数える(小さく圧縮した
        巨大な本文も止める)。展開の前の受信量も同じ上限で止める(_SlicedStream)。展開器には
        受信データを小さく分けて渡し、上限の検査の前に巨大に展開しない。展開比を抑えられない
        符号化(重ねた符号化・br・zstd)は受けない。
        受信のたびに期限も見る(止まっている受信は _DeadlineGuard が終わらせる)。
        """
        codings = [
            coding.strip().lower()
            for coding in response.headers.get_list("content-encoding", split_commas=True)
        ]
        expanding = [coding for coding in codings if coding in _EXPANDING_CODINGS]
        if len(expanding) > 1 or not set(expanding) <= {"gzip", "deflate"}:
            raise FatalError(
                f"展開後の大きさを抑えられない Content-Encoding({', '.join(expanding)}): {url}"
            )
        response.stream = _SlicedStream(response.stream, url)
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

    def _check_target(self, url: str, deadline: float) -> str:
        """接続先を検証し、接続に使う検証済み IP を返す。"""
        host = urlsplit(url).hostname
        if not host:
            raise FatalError(f"ホストのない URL: {url[:80]}")
        try:
            addresses = self._resolve_within(host, deadline, url)
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

    def _resolve_within(self, host: str, deadline: float, url: str) -> list[str]:
        """名前解決を期限の残りまでしか待たない(GH#203)。

        getaddrinfo は途中で止められないため別のスレッドで引き、期限が来たら待つのをやめる。
        残ったスレッドは OS の resolver の timeout で終わる(daemon なので終了も妨げない)。
        止まった名前解決が積み上がらないよう、同時に走るスレッドが上限まで埋まっていれば
        新しく作らずに名前解決の失敗にする(GH#188 と同じく再試行するが、スレッドは増えない)。
        """
        outcome: list[list[str] | BaseException] = []
        slots = _resolver_slots

        def resolve() -> None:
            try:
                outcome.append(self._resolve_host(host))
            except BaseException as e:  # 呼び出し元のスレッドで送出し直す
                outcome.append(e)
            finally:
                slots.release()

        remaining = self._remaining(deadline, url)
        if not slots.acquire(blocking=False):
            raise DnsResolutionError(f"DNS 解決失敗: {host}(止まった名前解決が上限まで残っている)")
        worker = threading.Thread(target=resolve, name="resolve-host", daemon=True)
        try:
            worker.start()
        except BaseException:
            slots.release()
            raise
        worker.join(remaining)
        if not outcome:
            raise FetchDeadlineError(
                f"取得の期限({FETCH_DEADLINE_S}s)内に名前解決が終わらない: {host}"
            )
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]
