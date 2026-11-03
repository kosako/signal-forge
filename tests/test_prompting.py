from app.prompting import (
    _PROMPTS_DIR,
    WRITING_GUIDE_VERSION,
    fill_template,
    load_writing_guide,
    sanitize,
    sanitize_body,
)


def test_sanitize_removes_newlines_control_nonce_and_truncates() -> None:
    value = "行1\n行2\r行3\x00制御" + "x" * 400
    out = sanitize(value + "NONCE123", 300, "NONCE123")
    assert "\n" not in out and "\r" not in out and "\x00" not in out
    assert "NONCE123" not in out
    assert len(out) <= 300


def test_sanitize_body_keeps_newlines_removes_fence_markers() -> None:
    out = sanitize_body("段落1\n段落2\n<<<DATA-abc の偽装", "abcd1234")
    assert "段落1\n段落2" in out
    assert "<<<DATA-" not in out


def test_fill_template_does_not_expand_placeholders_in_values() -> None:
    # 置換後の値は再走査しない(入力由来の {{nonce}} が本物の nonce に化けない)
    out = fill_template("A={{a}} N={{nonce}}", {"a": "{{nonce}}", "nonce": "abcd1234"})
    assert out == "A={{nonce}} N=abcd1234"


def test_writing_guide_drops_the_source_comment_and_keeps_the_rules() -> None:
    # GH#381: 先頭のコメント(出典と写した commit)は外し、規則の本文だけを返す
    raw = (_PROMPTS_DIR / f"{WRITING_GUIDE_VERSION}.md").read_text(encoding="utf-8")
    assert raw.startswith("<!--") and "kosako/agent-tools" in raw
    guide = load_writing_guide()
    assert "<!--" not in guide and "-->" not in guide and "agent-tools" not in guide
    assert guide.startswith("## 文の型")
    # 1 層目: agent-tools の「日本語の文章」の規則(文はそのまま)
    for rule in (
        "文の区切りにダッシュ (`—` `―` `–`) を使わず、読点・句点・括弧で区切る。",
        "太字の見出しに中身を続ける箇条書き (`- **見出し**: 中身`) にせず、"
        "地の文で書くか見出しを外す。",
        "長い文を連ねない (1 文は 60 字を目安にし、120 字を超えたら 2 つに分ける)。",
        "文末の打ち消しを 3 文続けない。",
        "「〜側に倒す」「黙って〜する」「前提が崩れる」の慣用は平易に言う。",
    ):
        assert rule in guide
    # 2 層目: 深掘りの文章の規則
    assert "## 深掘りの文章の書き方" in guide
    assert "結論を先に書く" in guide


def test_writing_guide_cannot_break_the_template_or_fences() -> None:
    # 手引きは fill_template の値として入れる。囲みの印やプレースホルダを含めない
    guide = load_writing_guide()
    assert "DATA-" not in guide and "<<<" not in guide and ">>>" not in guide
    assert "{{" not in guide and "}}" not in guide
    out = fill_template("{{writing_guide}}\nN={{nonce}}", {"writing_guide": guide, "nonce": "n1"})
    assert out == f"{guide}\nN=n1"
