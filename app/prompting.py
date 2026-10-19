"""LLM に渡すプロンプトを組み立てる共通の部品(設計 03 §3.2。設計 00 §3・GH#113)。

分析・トレンド・深掘り・カードへの返事が同じ規約で使う:
- 外部由来の値は sanitize(1 行)・sanitize_body(本文)で制御文字・nonce・フェンス境界を落とす
- テンプレートは fill_template で 1 回だけ置換する(入力由来の {{...}} を展開しない)
"""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(value: str | None, max_length: int, nonce: str) -> str:
    """改行・制御文字の除去(1行強制)→ nonce 除去 → 長さ上限(設計 03 §3.2)。"""
    if not value:
        return ""
    flattened = _CONTROL_CHARS.sub(" ", value.replace("\n", " ").replace("\r", " "))
    return flattened.replace(nonce, "")[:max_length]


def sanitize_body(value: str, nonce: str) -> str:
    """body は改行を保持し、制御文字(改行以外)・nonce・フェンス境界を除去する。"""
    cleaned = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", value)
    cleaned = cleaned.replace(nonce, "")
    # フェンス境界の偽装文字列も除去(nonce 除去で実質無効化されるが、生の境界形も落とす)
    return cleaned.replace("<<<DATA-", "").replace("DATA-", "")


def fill_template(template: str, mapping: dict[str, str]) -> str:
    """単一パス置換: テンプレート上のプレースホルダだけを置き換え、置換後の値は再走査しない
    (逐次 .replace() だと入力由来の {{nonce}} 等が展開され nonce が漏れる)。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: mapping[m.group(1)], template)
