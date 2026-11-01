"""vault に書く untrusted な Markdown から、ノートを開いた時点の外部への読み込みを除く(GH#377)。

LLM の出力と記事の抽出本文は untrusted のデータで、画像の埋め込みや生の HTML を含みうる。
Obsidian はノートを開くだけで、それらの参照先(外部のホスト)を読みに行く。そこで vault に
書く前に、次のように直す(設計 04 §7・06 §4):

- 画像の埋め込みは、`!` を `\\!` にしてふつうのリンクとして見せる(参照形式 `![alt][ref]` も
  同じ)。宛先は 1 文字も書き換えない。開いただけでは読みに行かず、押せば開ける
- 埋め込みのまま残すのは、vault の中を指すと形から決まるものだけ。1 行の中の `![alt](宛先)` で、
  宛先が `<>` もスキームも無い相対パス(先頭が `/`・`\\` でなく、空白・`\\`・`:`・`&`・括弧・角括弧・
  バッククォートを含まない)、代替テキストに角括弧・バッククォート・`\\`・`<` が無く、タイトルが
  無いもの。`![[ノート]]` は、中身が URL の形(`:`・`//`・`\\` を含む)のときと、直後に `(`・`[`・
  `!` が続いてふつうの画像と読まれうるとき以外は残す
- 生の HTML のタグ・コメント・宣言・処理命令の始まりの `<` を `&lt;` にする。表示は同じ文字の
  まま、描画されない。`<https://…>` とメールアドレスの自動リンクは残す
- リンク `[text](url)`、裸の URL、自動リンクは変えない(押すまで読みに行かない)。宛先を `<…>` で
  囲んだリンクのようにまれな形は、`<` を escape するので文字として見えることがある
- コード(フェンスとインラインのコード)の中は HTML として描画されないので触らない。ただし
  コードかどうかの解釈が揺れうる形(字下げや入れ子のフェンス、閉じていないフェンス、表・数式・
  脚注・リンクの宛先と同じ段落のインラインのコードなど)は守らずに直す。そのときコードの中の表示が
  `&lt;` などになるだけで、読み込みは起きない

埋め込みも HTML も含まない入力は、改行の形(`\\r\\n` は `\\n` に揃える)を除いてそのまま返す。
同じ入力に 2 回かけても結果は変わらない。
"""

from __future__ import annotations

import re

_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

# `<` の後ろがこれなら、HTML のタグ・閉じタグ・コメント・宣言・処理命令の始まり
_TAG_START = re.compile(r"<[A-Za-z/!?]")
# 自動リンク(URL とメールアドレス。CommonMark の形)。リンクになるだけで読み込みは起きない
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\x00-\x20]*"
    r"|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*)>"
)
# 埋め込みのまま残す、vault の中の添付の画像の形(単純で解釈の揺れの無いものだけ)。宛先は
# スキームの `:`・先頭の `/`(`//` を含む)・`\`・実体参照の `&`・空白・括弧を含まない相対パス
_LOCAL_IMAGE = re.compile(r"!\[[^\[\]`\\<\n]*\]\([\w.~%+\-][\w.~/%+\-]*\)")
# `![[…]]` の直後が `(` / `[` なら、CommonMark では角括弧入りの代替テキストの画像になる。
# 直後の `![` と並ぶ形も、念のため残さない
_WIKI_EMBED = re.compile(r"!\[\[([^\[\]\n]*)\]\](?![(\[!])")
_URL_LIKE = re.compile(r":|//|\\")

# フェンスの行の形(行頭の字下げ・引用・リストの印の後ろに ``` / ~~~)。これに当たる行が
# 解釈の揺れの無い組になっていないときは、フェンスの中を守らない
_FENCE_LIKE = re.compile(r"[ \t>*+\-.)\d]*(?:`{3,}|~{3,})")
_FENCE_OPEN = re.compile(r"(`{3,}|~{3,})(.*)")
_BACKTICK_RUN = re.compile(r"`+")
# 同じまとまり(_neutralize_block)にあると、インラインのコードの区切りの解釈がコードの外の
# 規則に左右されうる形
# (リンク・参照の定義の宛先、wikilink、脚注の参照 `[^…]`・インラインの脚注 `^[…]`、表の区切り、
# 数式、Obsidian のコメント。脚注のラベルはインラインのコードより先に読まれうる)
_SPAN_AMBIGUOUS = re.compile(r"\]\(|\]:|\[\[|\[\^|\^\[|\||\$|%%")


# ノートの先頭の frontmatter(render_frontmatter の出力。値の中の改行の後ろは字下げされるので、
# 最初の `\n---\n` が終わり)
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def neutralize_note(content: str) -> str:
    """ノート全体のうち、先頭の frontmatter を除いた本文を neutralize_markdown で直す。

    frontmatter は YAML シリアライザの出力で、値は描画されないので触らない。frontmatter が
    無ければ全体を直す。neutralize_markdown は 2 回かけても変わらないので、組み立てのときに
    直した本文にもう一度かけてよい。
    """
    m = _FRONT_MATTER.match(content)
    head = m[0] if m else ""
    return head + neutralize_markdown(content[len(head) :])


def neutralize_markdown(text: str) -> str:
    """untrusted な Markdown の画像の埋め込みと生の HTML を、読み込みの起きない形に直す。"""
    if "\r" in text:  # CommonMark は \r も改行とみなす。行の区切りを揃えてから見る
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # Obsidian の数式・コメントのブロックの中ではフェンスの解釈が変わりうるので守らない
    in_fence = (
        [False] * len(lines) if "$$" in text or "%%" in text else _fenced_lines(lines)
    )
    out: list[str] = []
    block: list[str] = []  # 空行・フェンスで区切った行のまとまり(段落はこの中に収まる)
    for index, line in enumerate(lines):
        if in_fence[index] or line.strip(" \t") == "":
            # フェンスの中と空行(全角空白だけの行は段落の続きなので含めない)はそのまま
            out.extend(_neutralize_block(block))
            block = []
            out.append(line)
        else:
            block.append(line)
    out.extend(_neutralize_block(block))
    return "\n".join(out)


def _neutralize_block(block: list[str]) -> list[str]:
    """空行・フェンスの間の行を直す。

    インラインのコードを守るのは、まとまりのどの行も、バッククォートの組が行の中で閉じ、
    区切りの解釈を揺らす形を含まないときだけ。コードの区切りは段落の前後の行の内容にも
    左右されうる(前の行から続くコード・表のセル・リンクの宛先など)ため、1 行でも外れれば
    まとまり全体でコードを守らない。
    """
    spans = [_code_spans(line) for line in block]
    clean = all(
        line_spans is not None
        and _SPAN_AMBIGUOUS.search(line) is None
        and _AUTOLINK.search(line) is None
        for line, line_spans in zip(block, spans, strict=True)
    )
    return [
        _neutralize_line(line, (line_spans or []) if clean else [])
        for line, line_spans in zip(block, spans, strict=True)
    ]


def _fenced_lines(lines: list[str]) -> list[bool]:
    """コードのフェンスの中(区切りの行を含む)の行に True を立てる。

    守るのは、行頭(字下げなし)の ``` / ~~~ で開き、同じ文字・同じ長さだけの行で閉じる組が、
    ほかのフェンスの形の行を挟まずに並ぶときだけ。字下げ・引用・リストの中のフェンス、閉じて
    いないフェンス、中にフェンスの形の行を含むものが 1 つでもあれば、どれも守らない(どこが
    コードかの解釈が描画側とずれると、コードと見なして残した HTML が描画されうるため)。
    """
    none = [False] * len(lines)
    flags = list(none)
    opener: tuple[int, str, int] | None = None  # (行, 文字, 長さ)
    for index, line in enumerate(lines):
        if not _FENCE_LIKE.match(line):
            continue
        if opener is None:
            m = _FENCE_OPEN.fullmatch(line)
            if m is None or (m[1][0] == "`" and "`" in m[2]):
                return none
            opener = (index, m[1][0], len(m[1]))
            continue
        start, char, length = opener
        fence = line.rstrip(" \t")
        if fence != char * length:
            return none
        for k in range(start, index + 1):
            flags[k] = True
        opener = None
    return none if opener is not None else flags


def _code_spans(line: str) -> list[tuple[int, int]] | None:
    """行の中のインラインのコードの範囲。バッククォートの組が行の中で閉じなければ None。"""
    spans = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] in _ASCII_PUNCT:
            i += 2  # エスケープしたバッククォートはコードを開かない
            continue
        if ch != "`":
            i += 1
            continue
        run = _BACKTICK_RUN.match(line, i).end() - i
        close = next(
            (m.start() for m in _BACKTICK_RUN.finditer(line, i + run) if len(m[0]) == run),
            None,
        )
        if close is None:
            return None
        spans.append((i, close + run))
        i = close + run
    return spans


def _neutralize_line(line: str, protected: list[tuple[int, int]]) -> str:
    parts = []
    pos = 0
    for start, end in protected:
        parts.append(_neutralize_inline(line[pos:start]))
        parts.append(line[start:end])  # インラインのコードの中は描画されないのでそのまま
        pos = end
    parts.append(_neutralize_inline(line[pos:]))
    return "".join(parts)


def _neutralize_inline(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _ASCII_PUNCT:
            if _TAG_START.match(text, i + 1):
                out.append("&lt;")  # `\<` と表示は同じ。バックスラッシュの解釈に頼らない
            else:
                out.append(text[i : i + 2])
            i += 2
            continue
        if ch == "!" and text.startswith("[", i + 1):
            replaced, i = _neutralize_image(text, i)
            out.append(replaced)
            continue
        if ch == "<":
            m = _AUTOLINK.match(text, i)
            if m is not None:
                out.append(m[0])
                i = m.end()
                continue
            if _TAG_START.match(text, i):
                out.append("&lt;")
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _neutralize_image(text: str, i: int) -> tuple[str, int]:
    """`![` で始まる位置を直し、(出力, 次に読む位置)を返す。宛先は書き換えない。"""
    if text.startswith("[[", i + 1):
        m = _WIKI_EMBED.match(text, i)
        if m is not None and _URL_LIKE.search(m[1]) is None:
            return "!", i + 1  # vault の中のノート・添付の埋め込みは残す
        return "\\!", i + 1
    m = _LOCAL_IMAGE.match(text, i)
    if m is not None:
        return m[0], m.end()  # vault の中の添付は外部を読まない
    # それ以外は `!` を escape して、ふつうのリンク(と文字)として見せる。続く `[…](…)` は
    # 呼び出し側がそのまま読み進める
    return "\\!", i + 1
