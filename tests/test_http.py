import httpx
import pytest

from app.connectors.base import FatalError, RetryableError
from app.http import HttpClient, SsrfBlockedError

PUBLIC = {"example.com": ["93.184.216.34"], "cdn.example": ["93.184.216.35"]}


def make_client(handler, hosts=None, sleeps=None):
    hosts = {**PUBLIC, **(hosts or {})}

    def resolve(host: str) -> list[str]:
        if host not in hosts:
            raise OSError(f"unknown host {host}")
        return hosts[host]

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
    assert "DNS 解決失敗" in str(excinfo.value.__cause__)  # 原因は例外の連鎖に残る


def test_resolve_short_url_returns_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host", "").startswith("example.com"):
            return httpx.Response(301, headers={"location": "https://cdn.example/article"})
        return httpx.Response(200)

    assert (
        make_client(handler).resolve_short_url("https://example.com/x")
        == "https://cdn.example/article"
    )


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
