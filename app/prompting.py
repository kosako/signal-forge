"""LLM に渡すプロンプトを組み立てる共通の部品(設計 03 §3.2。設計 00 §3・GH#113)。

分析・トレンド・深掘り・カードへの返事が同じ規約で使う:
- 外部由来の値は sanitize(1 行)・sanitize_body(本文)で制御文字・nonce・フェンス境界を落とす
- テンプレートは fill_template で 1 回だけ置換する(入力由来の {{...}} を展開しない)
- 深掘りの文章の書き方の手引き(prompts/writing-guide-ja-*)は load_writing_guide で読む(GH#381)
"""

from __future__ import annotations

import re
from pathlib import Path

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 書き方の手引き(GH#381)。深掘りの文章を作るプロンプトが共通で入れる(dig-answer と dig。
# dig-revise は dig を含むので入る)。
# 版を上げるときは prompts/ に新しい番号のファイルを足し、この定数を更新する(既存の版は書き換えない)
# v002 は読む場所の規則(Telegram・Markdown)を外し、各プロンプトに持たせた(GH#381。解説ノートは
# Obsidian で読む Markdown のため)
WRITING_GUIDE_VERSION = "writing-guide-ja-v002"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# 手引きの先頭の HTML コメント(出典と写した commit の記録)。LLM には渡さない
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)


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


def load_writing_guide() -> str:
    """書き方の手引きの本文(GH#381)。先頭のコメントを外して返す。

    リポジトリの中の信頼される指示なので、fill_template の値としてそのまま入れる(nonce の囲みの
    外に置く)。値は再走査されないので、手引きの中の文字列がプレースホルダとして展開されることはない。
    """
    text = (_PROMPTS_DIR / f"{WRITING_GUIDE_VERSION}.md").read_text(encoding="utf-8")
    return _LEADING_COMMENT.sub("", text, count=1).strip()
