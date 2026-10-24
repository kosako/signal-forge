"""canonical_url 正規化(ルール u1)と content_hash(ルール h1)。設計 01 §4〜5。

いずれも**挿入前に1回だけ計算し以後不変**(UNIQUE キーの遡及再計算は dedup 衝突と
履歴断絶を生む)。ルール改定時は新バージョンを新規挿入分から適用する。

短縮 URL 展開(u1 手順1)だけは HTTP を要するため、resolver を注入する
(実体は共通 HTTP クライアント。設計 05 §8 の SSRF ガード付きで #7 が配線する)。
X 由来は API の entities.expanded_url を使うので resolver 不要。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

# u1 手順1: これらのドメインのみ 1 回の GET で最終 URL に解決する(全 URL は解決しない)
SHORT_URL_HOSTS = frozenset({"t.co", "bit.ly", "buff.ly", "goo.gl", "ow.ly", "tinyurl.com"})

# u1 手順5: 削除するトラッキング系クエリパラメータ
_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "s", "t"}
)

# u1 手順3: X のホストの別名。ブックマークの内部リンクの判定(connectors/x_signals)も
# 同じ集合を使う(GH#232)。www つきは GH#232 から(それより前の行は遡及しない)
X_HOSTS = frozenset(
    {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)
_X_STATUS_PATH = re.compile(r"^/(?:[A-Za-z0-9_]+|i/web)/status(?:es)?/(\d+)")

_URL_IN_TEXT = re.compile(r"https?://\S+")
_WHITESPACE = re.compile(r"\s+")

HASH_RULE_PREFIX = "h1|"


class UrlError(Exception):
    """u1 で reject される URL。呼び出し側はアイテムごと `status='error'` にする。"""


class UnsupportedSchemeError(UrlError):
    """http/https 以外の URL(u1 手順0)。"""


class InvalidUrlError(UrlError):
    """構造が不正・許容しない URL(分解できない・userinfo 付き・不正ポート等)。

    userinfo(`user:pass@`)は無言で落とすと別リソースの誤 dedup になり、保持すると
    canonical_url 経由で credential が配信・ノートへ漏れるため reject する。
    """


def normalize_url(url: str, *, resolve_short: Callable[[str], str] | None = None) -> str:
    """u1 の適用順(設計 01 §4)で canonical_url を返す。

    resolve_short: 既知短縮ドメインの最終 URL 解決(1回)。None なら展開しない
    (X は expanded_url 済み、テスト・オフライン時)。
    """
    split = _split_url(url.strip(), f"URL を分解できない: {url[:80]}")
    if split.scheme.lower() not in ("http", "https"):
        raise UnsupportedSchemeError(f"scheme が http/https でない: {url[:80]}")

    # 手順1: 既知短縮ドメインのみ解決し、結果 URL に対して以降の手順を適用
    if resolve_short is not None and split.hostname in SHORT_URL_HOSTS:
        unsplittable = f"短縮 URL の解決先を分解できない: {url[:80]}"
        try:
            target = resolve_short(url.strip())
        except ValueError as e:
            # 解決の途中(転送先の Location 等)で分解できない URL に当たった(GH#186)
            raise InvalidUrlError(unsplittable) from e
        resolved = _split_url(target, unsplittable)
        if resolved.scheme.lower() not in ("http", "https"):
            raise UnsupportedSchemeError(f"短縮 URL の解決先が http/https でない: {url[:80]}")
        split = resolved

    if split.username is not None or split.password is not None:
        raise InvalidUrlError(f"userinfo 付き URL は許容しない: {split.hostname or ''}")

    # 手順2: scheme・host 小文字化、デフォルトポート除去(scheme の書き換えはしない)
    scheme = split.scheme.lower()
    host = (split.hostname or "").lower()
    try:
        port = split.port
    except ValueError as e:
        raise InvalidUrlError(f"ポートが不正: {url[:80]}") from e
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    # 手順3: twitter.com 系 → x.com。X ポストは /i/web/status/{id} 形式に組み立てる
    if host in X_HOSTS:
        host = "x.com"
        m = _X_STATUS_PATH.match(split.path)
        if m:
            return f"https://x.com/i/web/status/{m.group(1)}"

    # 手順5: トラッキングパラメータ削除+キー昇順ソート(手順4 の fragment は組み立てで落とす)
    pairs = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if key not in _TRACKING_PARAMS and not key.startswith("utm_")
    ]
    query = urlencode(sorted(pairs))

    # 手順6: path 末尾の / を除去(root は除く)
    path = split.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")

    return urlunsplit((scheme, host, path, query, ""))


def _split_url(url: str, error_message: str) -> SplitResult:
    """入力 URL と短縮 URL の解決先の分解に共通。分解できなければ InvalidUrlError。

    urlsplit は IPv6 の括弧の不整合・NFKC で不正になる netloc などで ValueError を送出する。
    呼び出し側の隔離は UrlError 単位のため、素の ValueError のままだと不正な 1 件で
    ソース全体の保存がロールバックされる(GH#186)。
    """
    try:
        return urlsplit(url)
    except ValueError as e:
        raise InvalidUrlError(error_message) from e


def normalize_text_for_hash(title: str | None, body: str | None) -> str:
    """h1 の正規化テキスト(設計 01 §5)。dedup 適格判定(20字以上)にも使う。"""
    text = f"{title or ''}\n{body or ''}"
    text = unicodedata.normalize("NFKC", text).lower()
    text = _URL_IN_TEXT.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def content_hash(normalized_text: str) -> str:
    """`sha256:` + hex(sha256("h1|" + 正規化テキスト))。h1 はルールバージョン。"""
    digest = hashlib.sha256((HASH_RULE_PREFIX + normalized_text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def is_dedup_eligible(normalized_text: str) -> bool:
    """24h 内容 dedup の親・子になれるか(空または 20 字未満は対象外。設計 01 §6)。"""
    return len(normalized_text) >= 20
