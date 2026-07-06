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
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        return httpx.Response(200)

    client = make_client(handler, hosts={"internal.example": ["192.168.0.10"]})
    with pytest.raises(SsrfBlockedError):
        client.get("https://example.com/short")


def test_redirect_followed_and_permanent_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
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


def test_dns_failure_is_retryable() -> None:
    with pytest.raises(RetryableError, match="DNS"):
        make_client(lambda request: httpx.Response(200)).get("https://nxdomain.example/")


def test_resolve_short_url_returns_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://cdn.example/article"})
        return httpx.Response(200)

    assert (
        make_client(handler).resolve_short_url("https://example.com/x")
        == "https://cdn.example/article"
    )
