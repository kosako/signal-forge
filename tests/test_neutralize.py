"""vault に書く untrusted な Markdown の埋め込みと生の HTML を直す(GH#377)。"""

import pytest

from app.export.neutralize import neutralize_markdown, neutralize_note

# --- 画像の埋め込み ---


@pytest.mark.parametrize(
    "source",
    [
        "![図](https://e.example/a.png)",
        "![](http://e.example/a.png)",
        '![図](https://e.example/a.png "題")',
        "![図](//e.example/a.png)",
        "![図](/abs/a.png)",
        "![図](https://e.example/a_(1).png)",
        "![図](data:image/png;base64,AAAA)",
        "![図](file:///tmp/a.png)",
        # 宛先に実体参照(`&#58;` = `:`)があっても vault の中とはみなさない
        "![図](https&#58;//e.example/a.png)",
        "![図](\\\\host/a.png)",
        "![図](a\\_b.png)",
        "![図](a b.png)",
        "![図](a_(1).png)",  # 括弧のある宛先は vault の中でも残さない
        '![図](a.png "題")',  # タイトルつき
        "![図 `x`](a.png)",  # 代替テキストにバッククォート
        "![図](https://e.example/a.png",  # 閉じていない(抜粋の切り詰めなど)
        "![[図]](https://e.example/a.png)",
    ],
)
def test_image_becomes_link_with_destination_unchanged(source) -> None:
    # `!` を escape するだけ。宛先は 1 文字も書き換えない(ふつうのリンクとして押せる)
    assert neutralize_markdown(source) == "\\" + source


def test_image_in_sentence_and_reference_style() -> None:
    assert neutralize_markdown("前 ![a](https://e.example/1.png) 後") == (
        "前 \\![a](https://e.example/1.png) 後"
    )
    source = "![図][ref] と ![ref]\n\n[ref]: https://e.example/a.png"
    # 参照形式もふつうのリンクとして残る(定義は変えない)
    assert neutralize_markdown(source) == (
        "\\![図][ref] と \\![ref]\n\n[ref]: https://e.example/a.png"
    )


def test_image_over_lines_is_escaped() -> None:
    assert neutralize_markdown("![図\n](https://e.example/a.png)") == (
        "\\![図\n](https://e.example/a.png)"
    )


@pytest.mark.parametrize(
    "source",
    [
        "![図](attachments/a.png)",
        "![図](a%20b.png)",
        "![図](./a.png)",
        "![](添付/画像.png)",
        "![[ノート]]",
        "![[画像.png|200]]",
        "![[ノート#見出し]]",
    ],
)
def test_vault_local_embed_is_kept(source) -> None:
    assert neutralize_markdown(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # `<>` の宛先は単純な形ではないので埋め込みにしない。`<` は escape する
        ("![図](<attachments/a.png>)", "\\![図](&lt;attachments/a.png>)"),
        ("![図](<https://e.example/a b.png>)", "\\![図](&lt;https://e.example/a b.png>)"),
        # 二重の括弧(PR #378 のレビュー)。2 回かけても同じ形のまま
        ("![図](<a((1)).png>)", "\\![図](&lt;a((1)).png>)"),
        ("![図](a((1)).png)", "\\![図](a((1)).png)"),
    ],
)
def test_angle_form_image_is_not_embedded_and_stable(source, expected) -> None:
    out = neutralize_markdown(source)
    assert out == expected
    assert neutralize_markdown(out) == out


@pytest.mark.parametrize(
    "source", ["![[https://e.example/a.png]]", "![[//e.example/a.png]]", "![[C:\\a.png]]"]
)
def test_wiki_embed_with_url_is_escaped(source) -> None:
    assert neutralize_markdown(source) == "\\" + source


def test_bang_before_image_does_not_make_new_embed() -> None:
    out = neutralize_markdown("!![a](https://e.example/x.png) ![[a]]![b](https://e.example/y.png)")
    assert out == "!\\![a](https://e.example/x.png) \\![[a]]\\![b](https://e.example/y.png)"
    assert neutralize_markdown(out) == out


def test_html_in_image_alt_and_title_is_escaped() -> None:
    out = neutralize_markdown('![<b>x</b>](https://e.example/a.png "<i>t</i>")')
    assert out == '\\![&lt;b>x&lt;/b>](https://e.example/a.png "&lt;i>t&lt;/i>")'
    assert neutralize_markdown("![<b>x</b>](a.png)") == "\\![&lt;b>x&lt;/b>](a.png)"


# --- 生の HTML ---


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="https://e.example/a.png">',
        '<iframe src="https://e.example/"></iframe>',
        '<video src="https://e.example/v.mp4" autoplay></video>',
        '<audio src="https://e.example/a.mp3"></audio>',
        '<object data="https://e.example/o"></object>',
        '<embed src="https://e.example/e">',
        '<link rel="stylesheet" href="https://e.example/s.css">',
        "<style>body{background:url(https://e.example/b.png)}</style>",
        '<svg><image href="https://e.example/a.png"/></svg>',
        '<IMG SRC="https://e.example/a.png">',
        '<picture><source srcset="https://e.example/a.webp"></picture>',
        "<!-- comment -->",
        "<!DOCTYPE html>",
        "<?xml version='1.0'?>",
    ],
)
def test_html_tags_are_escaped(tag) -> None:
    out = neutralize_markdown(f"本文 {tag} 続き\n\n{tag}")
    assert "<" not in out
    assert out == f"本文 {tag} 続き\n\n{tag}".replace("<", "&lt;")


def test_backslash_escaped_tag_becomes_entity() -> None:
    # `\<` も表示は同じ `<`。バックスラッシュの解釈に頼らない
    assert neutralize_markdown("\\<img src=x>") == "&lt;img src=x>"


def test_html_in_blockquote_list_and_link_text_is_escaped() -> None:
    source = (
        "> [!quote]- 原文\n> <img src=https://e.example/a.png>\n"
        "- <iframe src=https://e.example/>\n[<b>x</b>](https://e.example/)"
    )
    out = neutralize_markdown(source)
    assert "<" not in out
    assert "[&lt;b>x&lt;/b>](https://e.example/)" in out


@pytest.mark.parametrize(
    "source",
    [
        "a < b と 1<2、<3",
        "<https://e.example/path?q=1>",
        "<mail@e.example>",
        "[リンク](https://e.example/a.png)",
        '[リンク](https://e.example/ "題")',
        "裸の URL https://e.example/a.png も そのまま",
        "**太字** と _斜体_ と `コード`",
        "> [!note] callout\n> 本文",
        "[[wikilink]] と [[ノート|別名]]",
        "- 一覧\n- [ ] タスク\n\n| a | b |\n|---|---|\n| 1 | 2 |",
        "\\[角括弧\\] と \\\\ と &amp; と &lt;b&gt;",
        "",
    ],
)
def test_text_without_embeds_or_html_is_unchanged(source) -> None:
    assert neutralize_markdown(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # 宛先は書き換えない。`<…>` の形はまれなので、`<` を escape して文字として見せる
        ("[a](<x.md>)", "[a](&lt;x.md>)"),
        ("[a](<x\\_y.md>)", "[a](&lt;x\\_y.md>)"),  # バックスラッシュも変えない
        ("[a]( <note.md>)", "[a]( &lt;note.md>)"),  # `(` の後ろの空白
        ("[a](<x(1).md>)", "[a](&lt;x(1).md>)"),
        # タグの形でない `<` と自動リンクの形は、宛先でもそのまま
        ("[a](<1.md>)", "[a](<1.md>)"),
        ("[a](<https://e.example/a>)", "[a](<https://e.example/a>)"),
    ],
)
def test_angle_link_destination_is_not_rewritten(source, expected) -> None:
    out = neutralize_markdown(source)
    assert out == expected
    assert "%" not in out
    assert neutralize_markdown(out) == out


# --- コード(描画されない場所は触らない) ---


def test_fenced_code_is_kept() -> None:
    source = (
        "前 <img src=x>\n\n```html\n<img src=\"https://e.example/a.png\">\n"
        "![x](https://e.example/a.png)\n```\n\n~~~\n<iframe>\n~~~\n後 <img src=y>"
    )
    out = neutralize_markdown(source)
    assert '```html\n<img src="https://e.example/a.png">\n![x](https://e.example/a.png)\n```' in out
    assert "~~~\n<iframe>\n~~~" in out
    assert out.startswith("前 &lt;img src=x>")
    assert out.endswith("後 &lt;img src=y>")


@pytest.mark.parametrize(
    "source",
    [
        "```\n<img src=x>",  # 閉じていない
        "   ```\n<img src=x>\n```\n<img src=y>\n```",  # 字下げのフェンス
        "1. a\n   ```\n<img src=x>\n```",  # リストの中のフェンス
        "> ```\n<img src=x>\n> ```",  # 引用の中のフェンス
        "````\n```\n<img src=x>\n````",  # 中にフェンスの形の行
        "```\n<img src=x>\n````",  # 閉じの長さが違う
        "```\n<img src=x>\n~~~",
        "$$\n```\n$$\n<img src=x>\n```",  # 数式のブロック
        "%%\n```\n%%\n<img src=x>\n```",  # Obsidian のコメント
        "```a```\n<img src=x>\n```",  # 情報文字列にバッククォート(フェンスではない)
        "```\r<img src=x>\r```\r<img src=y>\r```",  # \r だけの改行
    ],
)
def test_ambiguous_fences_are_not_trusted(source) -> None:
    out = neutralize_markdown(source)
    assert "<img" not in out


def test_inline_code_is_kept() -> None:
    source = "タグ `<img src=x>` と ``a ` <iframe> `` は <b>本文</b>"
    out = neutralize_markdown(source)
    assert out == "タグ `<img src=x>` と ``a ` <iframe> `` は &lt;b>本文&lt;/b>"


def test_inline_code_on_several_lines_of_a_paragraph() -> None:
    source = "1 行目 `<a>`\n2 行目 `<b>`\n\n次の段落 `<c>`"
    assert neutralize_markdown(source) == source


@pytest.mark.parametrize(
    "source",
    [
        "\\`<img src=x>`",  # escape したバッククォートはコードを開かない
        "`a\nb` <img src=x> `c`",  # 前の行から続くコード
        "# `a\nb` <img src=x> `c`",
        "| `a | <img src=x> | b` |",  # 表のセルの区切り
        "[a](x`y) <img src=x> `",  # リンクの宛先のバッククォート
        "[a](\nx`y) <img src=x> `",
        "<https://e.example/`> <img src=x> `",  # 自動リンクの中のバッククォート
        "[[a`b]] <img src=x> `",
        "$`$ <img src=x> $`$",
        "``a` <img src=x> `",  # 長さの合わない組
    ],
)
def test_ambiguous_inline_code_is_not_trusted(source) -> None:
    out = neutralize_markdown(source)
    assert "<img" not in out


@pytest.mark.parametrize(
    "source",
    [
        '[^a`b] <img src="https://e.example/p.png"> `\n\n[^a`b]: 注',
        "本文[^`x] <iframe src=https://e.example/> `",
    ],
)
def test_footnote_label_does_not_protect_inline_code(source) -> None:
    # 脚注の参照のラベルはインラインのコードより先に読まれうる(コードと見なさない)
    out = neutralize_markdown(source)
    assert "<img" not in out and "<iframe" not in out


def test_full_width_space_line_does_not_end_paragraph() -> None:
    # 全角空白だけの行は CommonMark では空行でない(段落が続く)
    out = neutralize_markdown("`a\n\u3000\nb` <img src=x> `c`")
    assert "<img" not in out


# --- 冪等・改行 ---


IDEMPOTENT_SAMPLES = [
    "![図](https://e.example/a.png) <img src=x> `<b>` ![r][ref] ![[https://e.example/x]]",
    "```\n<img>\n```\n<img>\n![a](<https://e.example/a b>)",
    "\\<img> [a](<b c>) ![a](a.png) [![a](https://e.example/i.png)](https://e.example/)",
    "`a\nb` <img src=x> `c`",
]


@pytest.mark.parametrize("source", IDEMPOTENT_SAMPLES)
def test_applying_twice_changes_nothing(source) -> None:
    once = neutralize_markdown(source)
    assert neutralize_markdown(once) == once


def test_crlf_is_normalized_and_kept_as_lines() -> None:
    assert neutralize_markdown("a\r\nb <i>\r\n") == "a\nb &lt;i>\n"


def test_neutralize_note_keeps_frontmatter_and_fixes_body() -> None:
    note = '---\ntitle: "<b>題</b>"\nurl: https://e.example/\n---\n\n![図](https://e.example/a.png)\n'
    assert neutralize_note(note) == (
        '---\ntitle: "<b>題</b>"\nurl: https://e.example/\n---\n\n'
        "\\![図](https://e.example/a.png)\n"
    )
    assert neutralize_note(neutralize_note(note)) == neutralize_note(note)
    # frontmatter の無いノートは全体を直す
    assert neutralize_note("<img src=x>") == "&lt;img src=x>"
