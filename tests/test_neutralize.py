"""vault に書く untrusted な Markdown の埋め込みと生の HTML を直す(GH#377)。"""

import pytest

from app.export.neutralize import neutralize_markdown, neutralize_note

# --- 画像の埋め込み ---


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("![図](https://e.example/a.png)", "[画像: 図](https://e.example/a.png)"),
        ("![](http://e.example/a.png)", "[画像](http://e.example/a.png)"),
        ('![図](https://e.example/a.png "題")', '[画像: 図](https://e.example/a.png "題")'),
        ("![図](//e.example/a.png)", "[画像: 図](//e.example/a.png)"),
        ("![図](<https://e.example/a b.png>)", "[画像: 図](https://e.example/a%20b.png)"),
        ("![図](https://e.example/a_(1).png)", "[画像: 図](https://e.example/a_(1).png)"),
        ("![図](data:image/png;base64,AAAA)", "[画像: 図](data:image/png;base64,AAAA)"),
        ("![図](file:///tmp/a.png)", "[画像: 図](file:///tmp/a.png)"),
        # 宛先に実体参照(`&#58;` = `:`)があっても vault の中とはみなさない
        ("![図](https&#58;//e.example/a.png)", "[画像: 図](https&#58;//e.example/a.png)"),
        ("前 ![a](https://e.example/1.png) 後", "前 [画像: a](https://e.example/1.png) 後"),
    ],
)
def test_remote_image_becomes_link(source, expected) -> None:
    assert neutralize_markdown(source) == expected


def test_reference_style_image_is_not_embedded() -> None:
    source = "![図][ref] と ![ref]\n\n[ref]: https://e.example/a.png"
    out = neutralize_markdown(source)
    # `!` を escape するので、参照形式はふつうのリンクとして残る(定義は変えない)
    assert out == "\\![図][ref] と \\![ref]\n\n[ref]: https://e.example/a.png"


@pytest.mark.parametrize(
    "source",
    [
        "![図](https://e.example/a b.png)",  # 宛先に空白(形が崩れている)
        "![図](https://e.example/a.png",  # 閉じていない(抜粋の切り詰めなど)
        "![図\n](https://e.example/a.png)",  # 行をまたぐ
        "![[図]](https://e.example/a.png)",
    ],
)
def test_unparsed_image_syntax_is_escaped(source) -> None:
    out = neutralize_markdown(source)
    assert "![" not in out.replace("\\![", "")


@pytest.mark.parametrize(
    "source",
    [
        "![図](attachments/a.png)",
        "![図](a%20b.png)",
        "![図](a_(1).png)",
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
        ("![図](<attachments/a.png>)", "![図](attachments/a.png)"),
        ("![図](<添付/a b.png>)", "![図](添付/a%20b.png)"),
        ("![図](<a(1).png>)", "![図](a(1).png)"),
    ],
)
def test_vault_local_embed_in_angle_form_is_kept(source, expected) -> None:
    # `<>` の形でも vault の中の添付は埋め込みのまま(`<` を残さない形にする)
    assert neutralize_markdown(source) == expected


@pytest.mark.parametrize(
    "source", ["![[https://e.example/a.png]]", "![[//e.example/a.png]]", "![[C:\\a.png]]"]
)
def test_wiki_embed_with_url_is_escaped(source) -> None:
    assert neutralize_markdown(source) == "\\" + source


def test_html_in_image_alt_and_title_is_escaped() -> None:
    out = neutralize_markdown('![<b>x</b>](https://e.example/a.png "<i>t</i>")')
    assert out == '[画像: &lt;b>x&lt;/b>](https://e.example/a.png "&lt;i>t&lt;/i>")'
    assert neutralize_markdown("![<b>x</b>](a.png)") == "![&lt;b>x&lt;/b>](a.png)"


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


def test_angle_link_destination_drops_brackets() -> None:
    assert neutralize_markdown("[a](<https://e.example/a b>)") == "[a](https://e.example/a%20b)"
    assert neutralize_markdown('[a](<https://e.example/a> "題")') == '[a](https://e.example/a "題")'
    # 括弧は対応が取れていればそのまま、取れていなければ % で表す(リンクのまま押せる)
    assert neutralize_markdown("[a](<x(1).md>)") == "[a](x(1).md)"
    assert neutralize_markdown("[a](<x(.md>)") == "[a](x%28.md)"
    # `)` で閉じない形は宛先と読まない(`<` は escape する。CommonMark でもリンクにならない)
    assert neutralize_markdown("[a](<img src=x>") == "[a](&lt;img src=x>"
    assert neutralize_markdown("[a](<b c> d)") == "[a](&lt;b c> d)"


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
        "[画像: 図](https://e.example/a.png)\n"
    )
    assert neutralize_note(neutralize_note(note)) == neutralize_note(note)
    # frontmatter の無いノートは全体を直す
    assert neutralize_note("<img src=x>") == "&lt;img src=x>"
