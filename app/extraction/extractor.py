"""本文抽出 — items から documents(TTL 付き本文キャッシュ)を生成する。

- 対象: status='new' かつ documents 未生成の items(duplicate/skipped/error は対象外)
- raw_payload の raw_content(RSS 全文フィード等)から十分な本文が取れれば HTTP を
  節約する。取れないときのみ共通 HTTP クライアント(SSRF ガード付き)で取得し、HTTP の
  charset と HTML 内の meta の宣言を考慮して復号する(GH#187)
- failed も documents 行を作って黙って脱落させない(本文なしでタイトル+メタだけの分析に
  回り、配信とノートに「本文を読めていない」と出る。設計 00 §6・GH#240)
- expires_at は挿入時に確定: ソース別 TTL(RSS/manual 30日・X 7日。設計 01 §6)
"""

from __future__ import annotations

import codecs
import json
import logging
import re
import sqlite3

import httpx
import trafilatura

from app.config import Config
from app.connectors.base import FatalError, RetryableError
from app.http import HttpClient

# これ未満の抽出結果は partial(本文の体をなしていない可能性が高く、分析品質に注意が要る)
MIN_OK_CHARS = 200

# HTML 内の文字コード宣言。宣言は <head> の冒頭に置かれるので先頭だけを、WHATWG の prescan と
# 同じく先頭から順に、コメント・タグ(引用符の中の値は値として読み飛ばす)・<! や <? の区切りに
# 分けて読む。<meta> の charset 属性か、http-equiv="Content-Type" の content 属性の charset=
# だけを使う(コメントの中・別の属性の値に含まれる宣言は使わない)
_META_SCAN_BYTES = 4096
_PRESCAN_RE = re.compile(
    rb"""<!--.*?(?:-->|\Z)"""
    rb"""|<(/?)([A-Za-z][^\s/>]*)((?:[^>"']|"[^"]*"|'[^']*')*)>?"""
    rb"""|<[!/?][^>]*>?""",
    re.DOTALL,
)
_ATTR_RE = re.compile(rb"""([^\s"'=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]*)))?""")
_CONTENT_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([^\s"';]+)""", re.IGNORECASE)
# 宣言は ASCII で書かれている。これを同じ文字として読めない codec(UTF-16/32・EBCDIC など)や
# 置換つきで復号できない codec(idna など)の宣言は誤りとして、次の宣言を見る(WHATWG が meta
# の UTF-16 の宣言を UTF-8 として扱うのと同じ考え)
_ASCII_PROBE = b'<meta charset="">'
# ラベルの読み替え(ブラウザが従う WHATWG Encoding に合わせる): Shift_JIS は Windows の拡張
# (①・㈱ など)を含む cp932 として読む。x-sjis / x-euc-jp は Python の codecs が知らない古いラベル
_LEGACY_LABELS = {"x-sjis": "cp932", "x-euc-jp": "euc_jp"}
_CODEC_OVERRIDES = {"shift_jis": "cp932"}


def extract_pending(
    conn: sqlite3.Connection,
    config: Config,
    http: HttpClient,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> dict[str, int]:
    """未抽出の items を処理し、status 別件数を返す。アイテム単位でエラー隔離する。"""
    rows = conn.execute(
        """
        SELECT items.*, sources.type AS source_type FROM items
        JOIN sources ON sources.id = items.source_id
        WHERE items.status = 'new'
          AND NOT EXISTS (SELECT 1 FROM documents WHERE documents.item_id = items.id)
        ORDER BY items.id
        """
    ).fetchall()

    counts = {"ok": 0, "partial": 0, "failed": 0}
    for row in rows:
        try:
            status = _extract_item(conn, config, http, row)
        except Exception as e:  # 予期しない例外(壊れた raw_payload 等)も隔離して続行
            _insert_document(
                conn, config, row, text=None, status="failed", error=f"{type(e).__name__}: {e}"
            )
            status = "failed"
        counts[status] += 1
        if status == "failed":
            logger.warning(
                "本文抽出に失敗(本文なしで分析に回る)",
                extra={"run_id": run_id, "stage": "extract", "item_id": row["id"]},
            )
    logger.info(
        "extract 完了",
        extra={"run_id": run_id, "stage": "extract", "extra": counts},
    )
    return counts


def _extract_item(
    conn: sqlite3.Connection, config: Config, http: HttpClient, row: sqlite3.Row
) -> str:
    text: str | None = None
    error: str | None = None

    raw_content = _raw_content(row)
    if raw_content:
        # 全文フィード等の同梱本文から先に試み、十分ならネットワークに出ない
        text = _extract_text(raw_content)

    if not _is_sufficient(text):
        fetched_text, error = _fetch_and_extract(http, row["canonical_url"])
        # HTTP 側が失敗しても raw_content 由来の短い本文があればそちらを使う
        if fetched_text is not None and len(fetched_text) > len(text or ""):
            text = fetched_text

    status = ("ok" if len(text) >= MIN_OK_CHARS else "partial") if text else "failed"
    # partial でも HTTP 失敗の経緯は記録する(「短い記事」と「取得失敗して raw_content に
    # 劣化」を DB 上で区別できるようにする)
    _insert_document(conn, config, row, text=text, status=status, error=error)
    return status


def _insert_document(
    conn: sqlite3.Connection,
    config: Config,
    row: sqlite3.Row,
    *,
    text: str | None,
    status: str,
    error: str | None,
) -> None:
    ttl_days = (
        config.retention.x_cache_ttl_days
        if row["source_type"] == "x"
        else config.retention.cache_ttl_days
    )
    conn.execute(
        """
        INSERT INTO documents (item_id, extracted_text, extraction_status, extraction_error,
                               expires_at)
        VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?))
        """,
        (row["id"], text, status, error, f"+{ttl_days} days"),
    )


def _raw_content(row: sqlite3.Row) -> str | None:
    if not row["raw_payload"]:
        return None
    return json.loads(row["raw_payload"]).get("raw_content")


def _extract_text(html: str) -> str | None:
    # 本文抽出(boilerplate 除去)を先に試み、短い断片や平文で None になったら
    # タグ除去の全文テキスト化(html2txt)、素の平文はそのまま本文扱いにする
    # (X の full_text・短い RSS summary を黙って落とさない)
    extracted = trafilatura.extract(html, include_comments=False)
    if extracted and extracted.strip():
        return extracted.strip()
    # html2txt は断片(<p>…</p> のみ等)だと空を返すため、完全な文書に包んで渡す
    stripped = trafilatura.html2txt(f"<html><body>{html}</body></html>")
    if stripped and stripped.strip():
        return stripped.strip()
    if "<" not in html:
        return html.strip() or None
    return None


def _is_sufficient(text: str | None) -> bool:
    return text is not None and len(text) >= MIN_OK_CHARS


def _fetch_and_extract(http: HttpClient, url: str) -> tuple[str | None, str | None]:
    """(抽出テキスト, エラー文字列)。失敗は握りつぶさず文字列で返して呼び出し側が記録する。"""
    try:
        fetched = http.get(url)
    except (RetryableError, FatalError) as e:
        return None, f"{type(e).__name__}: {e}"
    if fetched.response.status_code != 200:
        return None, f"HTTP {fetched.response.status_code}"
    return _extract_text(_decode_html(fetched.response)), None


def _decode_html(response: httpx.Response) -> str:
    """本文のバイト列を、HTTP の charset と HTML 内の宣言を考慮して復号する(GH#187)。

    response.text はヘッダに charset が無いと UTF-8 で復号するため、meta だけで Shift_JIS 等を
    宣言したページが U+FFFD だらけになり、それでも長さで ok と判定されていた。
    順序: HTTP の charset → (ヘッダに無ければ)非 ASCII を含む正しい UTF-8 ならそのまま(従来と
    同じ結果。ほかの文字コードの本文が偶然 UTF-8 として正しくなることはまず無く、宣言の誤りより
    確か)→ HTML 内の meta の宣言(全バイトが ASCII の ISO-2022-JP もここで読む)→ 従来どおり
    UTF-8(読めないバイトは置換)
    """
    content = response.content
    codec = _codec(response.charset_encoding)
    if codec is None:
        if not content.isascii():
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        codec = _meta_codec(content[:_META_SCAN_BYTES])
    decoded = _decode(content, codec) if codec is not None else None
    return decoded if decoded is not None else content.decode("utf-8", errors="replace")


def _meta_codec(head: bytes) -> str | None:
    """HTML の先頭にある、使える最初の文字コード宣言の codec 名。無ければ None。"""
    for token in _PRESCAN_RE.finditer(head):
        end_tag, name, attr_text = token.groups()
        if name is None or end_tag or name.lower() != b"meta":
            continue
        attrs: dict[bytes, bytes] = {}
        for attr in _ATTR_RE.finditer(attr_text):
            value = attr.group(2) or attr.group(3) or attr.group(4) or b""
            attrs.setdefault(attr.group(1).lower(), value)  # 同じ名前は最初を使う(WHATWG と同じ)
        label = attrs.get(b"charset")
        if not label and attrs.get(b"http-equiv", b"").strip().lower() == b"content-type":
            match = _CONTENT_CHARSET_RE.search(attrs.get(b"content", b""))
            label = match.group(1) if match else None
        codec = _codec(label.decode("latin-1")) if label else None
        if codec is not None and _decode(_ASCII_PROBE, codec) == _ASCII_PROBE.decode("ascii"):
            return codec
    return None


def _codec(label: str | None) -> str | None:
    """文字コードのラベルを復号に使う codec 名にする。ラベルが無い・未知なら None。"""
    if not label:
        return None
    label = label.strip().lower()
    try:
        name = codecs.lookup(_LEGACY_LABELS.get(label, label)).name
    except (LookupError, ValueError):  # ValueError: NUL を含むラベル
        return None
    return _CODEC_OVERRIDES.get(name, name)


def _decode(content: bytes, codec: str) -> str | None:
    """読めないバイトは置換して復号する。この使い方ができない codec(rot13・idna 等)なら None。"""
    try:
        return content.decode(codec, errors="replace")
    except (LookupError, UnicodeError):
        return None
