import contextlib
import gzip
import socket
import threading
import time
import tracemalloc
import zlib

import httpx
import pytest

import app.http
from app.connectors.base import FatalError, RetryableError
from app.http import (
    FETCH_DEADLINE_S,
    MAX_RESPONSE_BYTES,
    MAX_RETRY_AFTER_S,
    READ_TIMEOUT_S,
    FetchDeadlineError,
    HttpClient,
    ResponseTooLargeError,
    SsrfBlockedError,
)

PUBLIC = {"example.com": ["93.184.216.34"], "cdn.example": ["93.184.216.35"]}


class FakeClock:
    """取得の期限の検査用の時計。待ちと、テストが決めた応答の所要時間で進む(GH#203)。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ChunkStream(httpx.SyncByteStream):
    """本文を chunk ごとに返す応答。count が None なら終わらない。受信のたびに on_chunk を呼ぶ。"""

    def __init__(self, chunk: bytes, count: int | None = None, on_chunk=None) -> None:
        self.chunk = chunk
        self.count = count
        self.on_chunk = on_chunk
        self.sent = 0

    def __iter__(self):
        while self.count is None or self.sent < self.count:
            self.sent += 1
            if self.on_chunk is not None:
                self.on_chunk()
            yield self.chunk


def make_client(handler, hosts=None, sleeps=None, clock=None):
    hosts = {**PUBLIC, **(hosts or {})}

    def resolve(host: str) -> list[str]:
        if host not in hosts:
            raise OSError(f"unknown host {host}")
        return hosts[host]

    if clock is not None:
        return HttpClient(
            transport=httpx.MockTransport(handler),
            resolve_host=resolve,
            sleep=clock.sleep,
            clock=clock,
        )
    return HttpClient(
        transport=httpx.MockTransport(handler),
        resolve_host=resolve,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


def test_simple_get() -> None:
    client = make_client(lambda request: httpx.Response(200, text="ok"))
    fetched = client.get("https://example.com/feed")
    assert fetched.response.status_code == 200
    assert fetched.final_url == "https://example.com/feed"
    assert fetched.permanently_moved is False


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.1.1",
        "100.64.0.1",  # shared address space(CGNAT)。ブラックリスト方式では素通りする帯
        "224.0.0.1",  # multicast
        "0.0.0.0",
        "::1",
        "fe80::1",
    ],
)
def test_ssrf_blocked_addresses(address: str) -> None:
    client = make_client(
        lambda request: httpx.Response(200), hosts={"evil.example": [address]}
    )
    with pytest.raises(SsrfBlockedError):
        client.get("https://evil.example/")


def test_redirect_hop_is_rechecked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        return httpx.Response(200)

    client = make_client(handler, hosts={"internal.example": ["192.168.0.10"]})
    with pytest.raises(SsrfBlockedError):
        client.get("https://example.com/short")


def test_redirect_followed_and_permanent_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(301, headers={"location": "https://cdn.example/new"})
        return httpx.Response(200, text="moved")

    fetched = make_client(handler).get("https://example.com/old")
    assert fetched.final_url == "https://cdn.example/new"
    assert fetched.permanently_moved is True


def test_redirect_loop_fails() -> None:
    handler = lambda request: httpx.Response(302, headers={"location": "https://example.com/a"})  # noqa: E731
    with pytest.raises(FatalError, match="リダイレクト"):
        make_client(handler).get("https://example.com/a")


def test_retry_on_500_then_success() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500) if len(calls) == 1 else httpx.Response(200)

    sleeps: list[float] = []
    fetched = make_client(handler, sleeps=sleeps).get("https://example.com/")
    assert fetched.response.status_code == 200
    assert len(calls) == 2
    assert 1.0 <= sleeps[0] < 2.0  # バックオフ1回目 = 1s + jitter


def test_retries_exhausted_raises_retryable() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(RetryableError, match="リトライ上限"):
        make_client(handler, sleeps=[]).get("https://example.com/")
    assert len(calls) == 3  # 初回 + 2回


def test_permanent_4xx_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    fetched = make_client(handler).get("https://example.com/")
    assert fetched.response.status_code == 404  # 業務分類は呼び出し側
    assert len(calls) == 1


def test_429_respects_retry_after() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200)

    sleeps: list[float] = []
    fetched = make_client(handler, sleeps=sleeps).get("https://example.com/")
    assert fetched.response.status_code == 200
    assert sleeps == [7]  # Retry-After 優先。追加バックオフを重ねない


def test_429_retry_after_at_cap_is_waited() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": str(MAX_RETRY_AFTER_S)})
        return httpx.Response(200)

    clock = FakeClock()
    fetched = make_client(handler, clock=clock).get("https://example.com/")
    assert fetched.response.status_code == 200
    assert clock.sleeps == [MAX_RETRY_AFTER_S]  # 上限ちょうどは待つ


def test_429_long_retry_after_fails_without_waiting() -> None:
    # 長い Retry-After をそのまま待つと朝バッチが止まる。待たずにそのソース・記事の失敗にする
    # (GH#203)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": str(MAX_RETRY_AFTER_S + 1)})

    clock = FakeClock()
    with pytest.raises(RetryableError, match="Retry-After") as excinfo:
        make_client(handler, clock=clock).get("https://example.com/")
    assert not isinstance(excinfo.value, FetchDeadlineError)
    assert clock.sleeps == []
    assert len(calls) == 1


def test_429_retry_after_beyond_deadline_fails_without_waiting() -> None:
    # 上限内の Retry-After でも、待つと期限までに試す時間が残らないなら待たない(GH#203)
    calls = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        clock.now += FETCH_DEADLINE_S - MAX_RETRY_AFTER_S  # 応答に時間がかかった
        return httpx.Response(429, headers={"retry-after": str(MAX_RETRY_AFTER_S)})

    with pytest.raises(FetchDeadlineError, match="期限"):
        make_client(handler, clock=clock).get("https://example.com/")
    assert clock.sleeps == []
    assert len(calls) == 1


def test_backoff_beyond_deadline_is_not_slept() -> None:
    # 試行ごとに 29s かかる 5xx。2 回目の後のバックオフ(4s+)で期限を越えるので待たない(GH#203)
    calls = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        clock.now += 29
        return httpx.Response(503)

    with pytest.raises(FetchDeadlineError, match="期限"):
        make_client(handler, clock=clock).get("https://example.com/")
    assert len(calls) == 2
    assert len(clock.sleeps) == 1
    assert clock.now < FETCH_DEADLINE_S


def test_request_timeout_shrinks_to_remaining_time() -> None:
    # 1 回の接続・受信の待ちも期限の残りで縮める。read 30s を毎回待たせて期限を越えない(GH#203)
    clock = FakeClock()
    timeouts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if request.headers["host"] == "example.com":
            clock.now += FETCH_DEADLINE_S - 5
            return httpx.Response(302, headers={"location": "https://cdn.example/new"})
        return httpx.Response(200)

    make_client(handler, clock=clock).get("https://example.com/old")
    assert timeouts[0]["read"] == READ_TIMEOUT_S
    assert timeouts[1] == {"connect": 5, "read": 5, "write": 5, "pool": 5}


def test_deadline_stops_redirect_chain() -> None:
    # リダイレクトの追跡も期限に含める。期限を過ぎたら次のホップへ接続しない(GH#203)
    clock = FakeClock()
    hosts: list[str] = []
    resolved: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        clock.now += FETCH_DEADLINE_S + 1
        return httpx.Response(302, headers={"location": "https://cdn.example/new"})

    def resolve(host: str) -> list[str]:
        resolved.append(host)
        return PUBLIC[host]

    client = HttpClient(
        transport=httpx.MockTransport(handler),
        resolve_host=resolve,
        sleep=clock.sleep,
        clock=clock,
    )
    with pytest.raises(FetchDeadlineError, match="cdn.example"):
        client.get("https://example.com/old")
    assert hosts == ["example.com"]  # 期限の超過はリトライしない
    assert resolved == ["example.com"]  # 次のホップの名前解決もしない


def test_deadline_message_keeps_dns_failure_host() -> None:
    # 期限で打ち切ったときも、DNS で失敗したホストを文言に残す(GH#188 の契約。GH#203)
    clock = FakeClock()

    def resolve(host: str) -> list[str]:
        clock.now += FETCH_DEADLINE_S / 2  # 名前解決に時間がかかった
        raise OSError("temporary failure in name resolution")

    client = HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolve_host=resolve,
        sleep=clock.sleep,
        clock=clock,
    )
    with pytest.raises(FetchDeadlineError) as excinfo:
        client.get("https://slow-dns.example/")
    assert "DNS 解決失敗: slow-dns.example" in str(excinfo.value)


def test_oversized_body_stops_reading_and_is_not_retried() -> None:
    # 本文はストリーミングで受け、上限を超えた時点で読むのをやめる(GH#203)
    calls = []
    stream = ChunkStream(b"x" * (1024 * 1024), count=2 * MAX_RESPONSE_BYTES // (1024 * 1024))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, stream=stream)

    with pytest.raises(ResponseTooLargeError, match="上限"):
        make_client(handler, sleeps=[]).get("https://example.com/huge")
    assert stream.sent == MAX_RESPONSE_BYTES // (1024 * 1024) + 1  # 超えた chunk で止まる
    assert len(calls) == 1  # 読み直しても同じなのでリトライしない
    assert issubclass(ResponseTooLargeError, FatalError)  # 呼び出し側の失敗の扱いに乗る


def test_body_at_limit_is_returned() -> None:
    body = b"x" * MAX_RESPONSE_BYTES
    fetched = make_client(lambda request: httpx.Response(200, content=body)).get(
        "https://example.com/big"
    )
    assert fetched.response.content == body


def test_compressed_body_is_limited_while_decoding() -> None:
    # 小さく圧縮した巨大な本文も展開後のバイト数で止める。1 回の受信分(ここでは約 100 KB)を
    # 一度に展開すると 100 MiB になるので、少しずつ展開してメモリを使い切らないこと(GH#203)
    compressor = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    block = bytes(1024 * 1024)
    bomb = b"".join(compressor.compress(block) for _ in range(100)) + compressor.flush()
    assert len(bomb) < 200 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(bomb)
        )

    client = make_client(handler)
    tracemalloc.start()
    try:
        with pytest.raises(ResponseTooLargeError):
            client.get("https://example.com/bomb")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < MAX_RESPONSE_BYTES * 3


def test_data_after_compressed_end_is_limited() -> None:
    # 展開器は gzip の終わりの後ろに届いたデータを、出力なしで内部にためる。展開後のバイト数は
    # 増えないので、展開の前の受信量も上限で止める(GH#203)
    head = gzip.compress(b"<rss/>")
    trailing = ChunkStream(bytes(1024 * 1024), count=2 * MAX_RESPONSE_BYTES // (1024 * 1024))

    class GzipThenTrailing(httpx.SyncByteStream):
        def __iter__(self):
            yield head
            yield from trailing

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=GzipThenTrailing())

    with pytest.raises(ResponseTooLargeError, match="上限"):
        make_client(handler).get("https://example.com/trailing")
    # gzip の分があるので、後ろのデータの 10 MiB 目で上限を超えて読むのをやめる
    assert trailing.sent == MAX_RESPONSE_BYTES // (1024 * 1024)


@pytest.mark.parametrize("encoding", ["gzip, gzip", "deflate, gzip", "br", "zstd"])
def test_unboundable_content_encoding_is_rejected(encoding: str) -> None:
    # 重ねた符号化は 1 段目の展開分をまとめて次の段で展開し、br / zstd は展開比が大きい。
    # 展開後の大きさを抑えられないので、展開せずにそのソース・記事の失敗にする(GH#203)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200, headers={"content-encoding": encoding}, stream=httpx.ByteStream(b"\x1f\x8b")
        )

    with pytest.raises(FatalError, match="Content-Encoding"):
        make_client(handler).get("https://example.com/bomb")
    assert len(calls) == 1


def test_unknown_content_encoding_passes_through() -> None:
    # httpx が展開しない誤った値(実在のサーバが送ることがある)は、従来どおり本文をそのまま返す
    fetched = make_client(
        lambda request: httpx.Response(
            200, headers={"content-encoding": "utf-8"}, content=b"<rss/>"
        )
    ).get("https://example.com/feed")
    assert fetched.response.content == b"<rss/>"


def test_accept_encoding_is_limited_to_gzip_and_deflate(monkeypatch) -> None:
    # httpx は br / zstd の展開器を入れると求める符号化に加える。入れた環境でも、上で受けない
    # 符号化を求めない(GH#203)
    monkeypatch.setattr(httpx._client, "ACCEPT_ENCODING", "gzip, deflate, br, zstd")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    make_client(handler).get("https://example.com/")
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


def test_compressed_body_within_limit_is_decoded() -> None:
    # ストリーミングで読んだ本文も、従来どおり展開した content / text として返す
    body = "本文".encode() * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "text/html; charset=utf-8"},
            stream=httpx.ByteStream(gzip.compress(body)),
        )

    fetched = make_client(handler).get("https://example.com/gz")
    assert fetched.response.content == body
    assert fetched.response.text == "本文" * 1000


def test_slow_body_stops_at_deadline() -> None:
    # 少しずつ送り続ける応答は read timeout では切れない。受信のたびに期限を見る(GH#203)。
    # 実際の接続は期限に切るので受信の途中で止まる(下の実ソケットのテスト)。ここでは
    # 受信の後の検査を見るため、chunk の到着で時計を進める
    clock = FakeClock()
    calls = []

    def advance() -> None:
        clock.now += READ_TIMEOUT_S - 5  # read timeout の内側で 1 chunk ずつ届く

    stream = ChunkStream(b"x", count=10, on_chunk=advance)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, stream=stream)

    with pytest.raises(FetchDeadlineError, match="期限"):
        make_client(handler, clock=clock).get("https://example.com/slow")
    assert stream.sent == 3  # 25s・50s・75s。期限 60s を越えた chunk で止まる
    assert len(calls) == 1  # 期限の超過はリトライしない
    assert clock.sleeps == []


def serve_once(respond) -> int:
    """127.0.0.1 で 1 回だけ応答する TCP サーバ。respond(conn) が応答を書く。port を返す。"""
    server = socket.create_server(("127.0.0.1", 0))

    def run() -> None:
        with server:
            conn, _ = server.accept()
            with conn, contextlib.suppress(OSError):  # クライアントが接続を切った
                conn.recv(65536)  # リクエスト
                respond(conn)

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def make_local_client(monkeypatch, deadline_s: float) -> HttpClient:
    # 実 transport で検査する。SSRF ガードは loopback を拒むので、接続先の検証だけを差し替える
    monkeypatch.setattr(app.http, "FETCH_DEADLINE_S", deadline_s)
    client = HttpClient(sleep=lambda _s: None)
    monkeypatch.setattr(client, "_check_target", lambda url, deadline: "127.0.0.1")
    return client


def test_slow_headers_are_cut_at_deadline(monkeypatch) -> None:
    # ヘッダを少しずつ送り続ける相手は read timeout では切れない。期限に接続を切る(GH#203)
    def respond(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        for _ in range(60):  # 0.1s ごとに 1 バイト。切られなければ 6s 続く
            conn.sendall(b"a")
            time.sleep(0.1)

    port = serve_once(respond)
    client = make_local_client(monkeypatch, 1.0)
    started = time.monotonic()
    with pytest.raises(FetchDeadlineError, match="を超過"):  # リトライの失敗にしない
        client.get(f"http://local.example:{port}/feed")
    assert time.monotonic() - started < 3.0


def test_body_cut_at_deadline_is_not_taken_as_complete(monkeypatch) -> None:
    # 接続を閉じて終わる本文(Content-Length なし)を期限で切ったとき、切った接続の終わりを
    # 本文の終わりと取り違えて途中までの本文を返さない(GH#203)
    def respond(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\npartial")
        time.sleep(0.3)
        conn.sendall(b"-more")
        time.sleep(3)  # 止まる。read timeout(期限の残り)より先に期限が来る

    port = serve_once(respond)
    client = make_local_client(monkeypatch, 1.0)
    with pytest.raises(FetchDeadlineError, match="期限"):
        client.get(f"http://local.example:{port}/article")


def test_slow_name_resolution_is_abandoned_at_deadline(monkeypatch) -> None:
    # 名前解決が返らなくても期限で待つのをやめる。失敗したホストは文言に残す(GH#188・GH#203)
    monkeypatch.setattr(app.http, "FETCH_DEADLINE_S", 0.5)
    release = threading.Event()

    def resolve(host: str) -> list[str]:
        release.wait(5)  # 応答しない resolver
        raise OSError("timed out")

    client = HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolve_host=resolve,
        sleep=lambda _s: None,
    )
    started = time.monotonic()
    try:
        with pytest.raises(FetchDeadlineError, match="slow-dns.example"):
            client.get("https://slow-dns.example/")
        assert time.monotonic() - started < 2.0
    finally:
        release.set()


def test_stalled_name_resolutions_do_not_pile_up(monkeypatch) -> None:
    # 期限で見捨てた名前解決のスレッドは止められない。返らない名前解決が続いても、スレッドを
    # 上限より増やさず、埋まっている間は待たずに名前解決の失敗にする(GH#188・GH#203)
    monkeypatch.setattr(app.http, "FETCH_DEADLINE_S", 0.2)
    monkeypatch.setattr(app.http, "_resolver_slots", threading.BoundedSemaphore(2))
    release = threading.Event()
    stalled: list[str] = []

    def resolve(host: str) -> list[str]:
        if host in PUBLIC:
            return PUBLIC[host]
        stalled.append(host)
        release.wait(10)  # 応答しない resolver
        raise OSError("timed out")

    client = HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolve_host=resolve,
        sleep=lambda _s: None,
    )
    try:
        for _ in range(2):  # 期限で見捨てたスレッドが上限の 2 本まで残る
            with pytest.raises(FetchDeadlineError, match="名前解決が終わらない"):
                client.get("https://slow-dns.example/")
        monkeypatch.setattr(app.http, "FETCH_DEADLINE_S", 60)
        started = time.monotonic()
        with pytest.raises(RetryableError, match="リトライ上限") as excinfo:
            client.get("https://slow-dns.example/")
        assert time.monotonic() - started < 2.0  # 名前解決を待たない
        assert "DNS 解決失敗: slow-dns.example" in str(excinfo.value)  # GH#188 の文言
        assert len(stalled) == 2  # GH#188 の再試行でもスレッドは増えない
    finally:
        release.set()
    for worker in threading.enumerate():
        if worker.name == "resolve-host":
            worker.join(10)
    # 止まっていた名前解決が終われば、また引ける
    assert client.get("https://example.com/").response.status_code == 200


def test_resolver_slot_is_returned_when_thread_cannot_start(monkeypatch) -> None:
    # 名前解決のスレッドを作れなかったときも枠を返す。返さないと枠が尽き、以後ずっと引けない
    monkeypatch.setattr(app.http, "_resolver_slots", threading.BoundedSemaphore(1))
    client = make_client(lambda request: httpx.Response(200))

    def cannot_start(self) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patched:
        patched.setattr(threading.Thread, "start", cannot_start)
        with pytest.raises(RuntimeError):
            client.get("https://example.com/")
    assert client.get("https://example.com/").response.status_code == 200


@pytest.mark.parametrize("empty_result", [False, True])
def test_transient_dns_failure_then_success(empty_result: bool) -> None:
    # 最初の名前解決だけが失敗し、次で回復する(スリープ復帰直後など。GH#188)
    resolved: list[str] = []

    def resolve(host: str) -> list[str]:
        resolved.append(host)
        if len(resolved) == 1:
            if empty_result:
                return []  # 解決結果が空
            raise OSError("temporary failure in name resolution")
        return PUBLIC[host]

    sleeps: list[float] = []
    client = HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolve_host=resolve,
        sleep=sleeps.append,
    )
    fetched = client.get("https://example.com/")
    assert fetched.response.status_code == 200
    assert resolved == ["example.com", "example.com"]
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] < 2.0  # 他の一時失敗と同じバックオフ1回目 = 1s + jitter


def test_persistent_dns_failure_exhausts_retries() -> None:
    resolved: list[str] = []

    def resolve(host: str) -> list[str]:
        resolved.append(host)
        raise OSError(f"unknown host {host}")

    sleeps: list[float] = []
    client = HttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        resolve_host=resolve,
        sleep=sleeps.append,
    )
    with pytest.raises(RetryableError, match="リトライ上限") as excinfo:
        client.get("https://nxdomain.example/")
    assert len(resolved) == 3  # 初回 + 2回(他の一時失敗と同じ回数)
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] < 2.0
    assert 4.0 <= sleeps[1] < 5.0  # 1s→4s + jitter
    # 呼び出し側は例外の型と文字列だけを記録するため、DNS の原因を文言に残す
    assert "DNS 解決失敗: nxdomain.example" in str(excinfo.value)


def test_dns_failure_on_redirect_hop_names_failed_host() -> None:
    # 記録される文字列に元 URL しか無いと、どのホップのホストが引けなかったか分からない
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "https://gone.example/a"})

    with pytest.raises(RetryableError, match="リトライ上限") as excinfo:
        make_client(handler, sleeps=[]).get("https://example.com/short")
    assert calls == ["example.com"] * 3  # 試行ごとに元 URL からたどり直す
    assert "DNS 解決失敗: gone.example" in str(excinfo.value)


def test_resolve_short_url_returns_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(301, headers={"location": "https://cdn.example/article"})
        return httpx.Response(200)

    assert (
        make_client(handler).resolve_short_url("https://example.com/x")
        == "https://cdn.example/article"
    )


def test_resolve_short_url_does_not_read_bodies() -> None:
    # 転送はヘッダで判定し、転送の応答・転送先の本文は読まない。転送先が上限を超える PDF
    # などでも URL は解決できる(GH#328)
    redirect_body = ChunkStream(b"x" * (1024 * 1024), count=None)
    final_body = ChunkStream(b"x" * (1024 * 1024), count=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(
                301, headers={"location": "https://cdn.example/big.pdf"}, stream=redirect_body
            )
        return httpx.Response(200, stream=final_body)

    assert (
        make_client(handler).resolve_short_url("https://example.com/x")
        == "https://cdn.example/big.pdf"
    )
    assert redirect_body.sent == 0
    assert final_body.sent == 0


def test_resolve_short_url_rechecks_each_hop() -> None:
    # 本文を読まなくても、各ホップの接続先の検証は get と同じに行う(GH#328)
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        return httpx.Response(200)

    client = make_client(handler, hosts={"internal.example": ["192.168.0.10"]})
    with pytest.raises(SsrfBlockedError):
        client.resolve_short_url("https://example.com/short")


def test_connection_pinned_to_validated_ip() -> None:
    # DNS rebinding 対策: 実接続は検証済み IP 宛て、Host ヘッダ+SNI は元ホスト名
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    make_client(handler).get("https://example.com/feed?q=1")

    request = seen[0]
    assert request.url.host == "93.184.216.34"  # 検証した IP に接続
    assert request.url.path == "/feed"
    assert request.url.query == b"q=1"
    assert request.headers["host"] == "example.com"
    assert request.extensions.get("sni_hostname") == "example.com"


def test_pinned_host_header_keeps_nondefault_port() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    make_client(handler).get("http://example.com:8080/x")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].url.port == 8080
    assert seen[0].headers["host"] == "example.com:8080"
    assert "sni_hostname" not in seen[0].extensions  # http は SNI 不要


def test_pinned_ipv6_is_bracketed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    make_client(
        handler, hosts={"v6.example": ["2606:2800:220:1:248:1893:25c8:1946"]}
    ).get("https://v6.example/a")
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert seen[0].headers["host"] == "v6.example"


def test_redirect_hops_are_each_pinned() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(302, headers={"location": "https://cdn.example/new"})
        return httpx.Response(200)

    make_client(handler).get("https://example.com/old")
    assert [r.url.host for r in seen] == ["93.184.216.34", "93.184.216.35"]
    assert [r.headers["host"] for r in seen] == ["example.com", "cdn.example"]


def test_invalid_port_is_fatal() -> None:
    with pytest.raises(FatalError, match="ポート"):
        make_client(lambda request: httpx.Response(200)).get("https://example.com:99999/")


def test_cookies_do_not_leak_across_hosts_pinned_to_same_ip() -> None:
    # 同一 IP に pin された別ホストへ cookie が流れない(cross-origin 状態共有の遮断)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "a.example":
            return httpx.Response(200, headers={"set-cookie": "session=secret"})
        return httpx.Response(200)

    client = make_client(
        handler, hosts={"a.example": ["93.184.216.34"], "b.example": ["93.184.216.34"]}
    )
    client.get("https://a.example/login")
    client.get("https://b.example/feed")
    client.get("https://a.example/again")  # 同一ホストの再訪でも毎回破棄

    assert all("cookie" not in request.headers for request in seen)


def test_keepalive_disabled_for_pinned_connections() -> None:
    # 接続プールのキーは IP origin になるため keep-alive を切る(TLS/SNI 再利用の防止)。
    # httpx は Limits を公開属性に持たないため、実 transport のプール設定で検証する
    pool = HttpClient()._client._transport._pool
    assert pool._max_keepalive_connections == 0
