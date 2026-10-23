import pytest

from app.normalize import (
    InvalidUrlError,
    UnsupportedSchemeError,
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

# --- normalize_url (u1) ---


def test_scheme_allowlist_rejects_javascript() -> None:
    with pytest.raises(UnsupportedSchemeError):
        normalize_url("javascript:alert(1)")


def test_scheme_allowlist_rejects_ftp() -> None:
    with pytest.raises(UnsupportedSchemeError):
        normalize_url("ftp://example.com/file")


def test_userinfo_url_rejected() -> None:
    # 無言で落とすと誤 dedup、保持すると credential 漏えいのため reject
    with pytest.raises(InvalidUrlError, match="userinfo"):
        normalize_url("https://alice:secret@example.com/a")


def test_invalid_port_rejected_as_url_error() -> None:
    with pytest.raises(InvalidUrlError, match="ポート"):
        normalize_url("https://example.com:bad/a")


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1",  # IPv6 の括弧が閉じていない
        "https://example.com]/a",  # 括弧の不整合
        "http://[abc]/x",  # 括弧の中が IP でない
        "https://ex\uff03ample.com/a",  # NFKC で不正になる netloc
    ],
)
def test_unsplittable_url_rejected_as_url_error(url) -> None:
    # urlsplit の ValueError を UrlError 系にそろえる(collect がアイテムごとに隔離できる。GH#186)
    with pytest.raises(InvalidUrlError, match="分解できない"):
        normalize_url(url)


def test_lowercases_scheme_and_host_only() -> None:
    assert normalize_url("HTTPS://Example.COM/Path/File") == "https://example.com/Path/File"


def test_removes_default_port_keeps_custom() -> None:
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"
    assert normalize_url("http://example.com:8080/a") == "http://example.com:8080/a"


def test_does_not_upgrade_http_to_https() -> None:
    assert normalize_url("http://example.com/a") == "http://example.com/a"


def test_twitter_hosts_unified_to_x() -> None:
    assert (
        normalize_url("https://mobile.twitter.com/someone/lists")
        == "https://x.com/someone/lists"
    )


def test_x_status_rebuilt_to_id_form() -> None:
    for url in (
        "https://twitter.com/someuser/status/12345",
        "https://x.com/Another_User/statuses/12345?s=20&t=abc",
        "https://x.com/i/web/status/12345",
    ):
        assert normalize_url(url) == "https://x.com/i/web/status/12345"


def test_fragment_removed() -> None:
    assert normalize_url("https://example.com/a#section") == "https://example.com/a"


def test_tracking_params_removed_and_rest_sorted() -> None:
    url = "https://example.com/a?utm_source=x&b=2&fbclid=f&a=1&ref=top"
    assert normalize_url(url) == "https://example.com/a?a=1&b=2"


def test_blank_query_values_kept() -> None:
    assert normalize_url("https://example.com/a?q=") == "https://example.com/a?q="


def test_trailing_slash_removed_except_root() -> None:
    assert normalize_url("https://example.com/a/b/") == "https://example.com/a/b"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_short_url_resolved_via_injected_resolver() -> None:
    resolved = normalize_url(
        "https://t.co/AbC123",
        resolve_short=lambda url: "https://example.com/Long/?utm_source=x",
    )
    assert resolved == "https://example.com/Long"


def test_non_short_host_not_resolved() -> None:
    def explode(url: str) -> str:
        raise AssertionError("resolver must not be called")

    assert normalize_url("https://example.com/a", resolve_short=explode) == "https://example.com/a"


def test_short_url_resolving_to_bad_scheme_rejected() -> None:
    with pytest.raises(UnsupportedSchemeError):
        normalize_url("https://t.co/x", resolve_short=lambda _: "javascript:alert(1)")


def test_short_url_resolving_to_unsplittable_url_rejected() -> None:
    # 解決先も入力 URL と同じく分解の失敗を UrlError 系にそろえる(GH#186)
    with pytest.raises(InvalidUrlError, match="短縮 URL の解決先を分解できない"):
        normalize_url("https://t.co/x", resolve_short=lambda _: "https://[::1/a")


# --- content_hash (h1) ---


def test_normalize_text_nfkc_lower_url_removal_whitespace() -> None:
    text = normalize_text_for_hash("ＡＢＣ Title", "本文  https://t.co/abc です\n\n改行")
    assert text == "abc title 本文 です 改行"


def test_content_hash_has_rule_prefix_and_is_stable() -> None:
    a = content_hash("abc title 本文")
    assert a.startswith("sha256:")
    assert a == content_hash("abc title 本文")
    assert a != content_hash("abc title 本文!")


def test_hash_differs_from_unprefixed_sha256() -> None:
    import hashlib

    text = "same text"
    assert content_hash(text) != f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def test_dedup_eligibility_threshold() -> None:
    assert is_dedup_eligible("x" * 20) is True
    assert is_dedup_eligible("x" * 19) is False
    assert is_dedup_eligible(normalize_text_for_hash(None, None)) is False  # manual 相当
