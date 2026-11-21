"""元の記事の言語の判定(GH#173・GH#439)。

- looks_japanese: 本文が日本語か。深掘りが原文ノートか対訳ノートかを決めるのに使う(GH#173。
  設計 06 §5.2)。迷うものは日本語以外(対訳)にする。英語の記事を日本語と誤ると対訳が無くなるため
- detect_language: 分析の時に、元の記事の言語を 1 つ決める(analyses.source_language。GH#439)。
  日本語にして出す記事に元の言語を示すための印なので、迷うものは None にする(印を出さない)。
  誤った言語を示すより、示さない方がよい
- どちらも文字の種類を数えるだけのコードの判定で、LLM には聞かない(prompt を変えずに、どの分析にも
  同じ規則で付けるため)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 判定の結果(analyses.source_language の CHECK と同じ)。other = 上のどれでもない言語
LANGUAGES = ("ja", "en", "zh", "ko", "other")
# 配信とノートに出す言語の名前。日本語の記事には印を出さないので ja は持たない
LANGUAGE_LABELS = {"en": "英語", "zh": "中国語", "ko": "韓国語", "other": "外国語"}
LANGUAGE_MARK = "🌐"

# 日本語の文字(かな・漢字)のうち、かながこの割合に満たなければ日本語にしない(かなは日本語に
# しか無い。漢字だけの中国語を外す。GH#173)
_JA_KANA_MIN_SHARE = 0.2
# 引用の括弧(開き → 閉じ)。地の文の言語の根拠にしない部分(GH#173)
_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”"}
# 開きと閉じが同じ字の二重引用符は、同じ行の中で前から 2 つずつ組にする
_ASCII_QUOTE_RE = re.compile(r'"[^"\n]*"')

# 中国語とみなす漢字の数の下限。漢字だけの短い日本語の見出し(「富士通、新製品発表」など)を
# 中国語と誤らないため、短いものは決めない
_ZH_MIN_HAN_CHARS = 20
# 本文ではなく、取り出しに失敗したファイルの中身(PDF のバイト列)。言語を決めない
_BINARY_PREFIXES = ("%PDF-",)
# URL は文の言語の根拠にしない(au・es・di のような短い部分が、ほかの言語の機能語と重なる)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _words(text: str) -> frozenset[str]:
    """空白で区切った語の並びを集合にする(1 語ずつ書くと縦に長くなるので、文字列で持つ)。"""
    return frozenset(text.split())


# 英語と、ローマ字を使うほかの言語(独・仏・西・葡・伊)を見分ける機能語。どちらにもある語
# (a・in・no など)と、1 字の語(西の y は「1Y」のような略記と重なる)は入れない。英語の方が多いか
# 同じなら英語にする(機能語の無い短い見出しは、ほとんどが英語の記事なので英語にする)
_EN_WORDS = _words(
    "the and of to for with is are was were be been by at from as that this it its you we "
    "they not how what why when your our can will has have had or an which their there into "
    "about more new using"
)
_OTHER_LATIN_WORDS = _words(
    "der die das und ist nicht mit ein eine den dem des sie wir auf für "
    "le la les et est une du au aux pour avec dans sur qui "
    "el los las que del por para una con es lo "
    "il di che gli della sono "
    "em não um uma das dos"
)
_LATIN_WORD_RE = re.compile(r"[^\W\d_]+")


def looks_japanese(text: str) -> bool:
    """本文が日本語か。引用を除いた地の文で、日本語の語の数がそれ以外の語の数より多ければ日本語。

    語 = 同じ文字種の文字の連なり(ひらがな・カタカナ・漢字は文字種ごとに区切る。それ以外は
    英単語やコードの識別子)。文字の数で比べないのは、日本語は 1 字あたりの情報が多く、英単語や
    コードの多い日本語の記事を取りこぼすため。引用(「」『』“” と同じ行の中の "…")の中は数えない:
    長い日本語の文を引用する英語の記事を、引用の語の数で日本語と誤らないため(日本語の記事は引用を
    除いても地の文が日本語のまま残る)。英語の記事を日本語と誤ると対訳が無くなるので、語の数が同じ
    ものや、コードが本文より多い日本語の記事のように迷うものは日本語以外(対訳)にする。
    """
    counts = _count_scripts(strip_quotes(text))
    if counts.kana_chars == 0 or counts.kana_chars < counts.ja_chars * _JA_KANA_MIN_SHARE:
        return False
    return counts.ja_words > counts.other_words


def detect_language(text: str | None) -> str | None:
    """元の記事の言語(LANGUAGES のどれか)。決められなければ None。

    1. 日本語(looks_japanese)なら ja
    2. 引用を除いた地の文に、かなが 1 字でもあれば None。かなは日本語にしか無いので、英語を長く
       引く日本語の記事や、X の画面の文字(Log in・Show translation など)に埋もれた日本語の投稿を、
       外国語と誤らない。かなを含む外国語の記事は印が出ないだけになる
    3. 語の数が一番多い文字の種類で決める。ハングルなら ko。漢字なら、漢字が _ZH_MIN_HAN_CHARS 字
       以上のとき zh(短いものは None)。ローマ字なら、漢字が混じらないときだけ、機能語で英語(en)か
       ほかの言語(other)かを決める。それ以外の文字(キリル文字・アラビア文字など)なら other。
       種類が拮抗すれば None
    2 と 3 では URL を除いて数える。文字が無い(空・数字と記号・URL だけ)ときと、PDF のバイト列
    (取り出しに失敗した中身)は None。
    """
    if not text or not text.strip() or text.lstrip().startswith(_BINARY_PREFIXES):
        return None
    if looks_japanese(text):
        return "ja"
    plain = _URL_RE.sub(" ", strip_quotes(text))
    counts = _count_scripts(plain)
    if counts.kana_chars > 0:
        return None
    by_script = {
        "ko": counts.hangul_words,
        "zh": counts.han_words,
        "latin": counts.latin_words,
        "other": counts.rest_words,
    }
    top = max(by_script.values())
    if top == 0:
        return None
    leaders = [name for name, count in by_script.items() if count == top]
    if len(leaders) > 1:
        return None  # 文字の種類が拮抗している(決めない)
    leader = leaders[0]
    if leader == "zh":
        return "zh" if counts.han_chars >= _ZH_MIN_HAN_CHARS else None
    if leader == "latin":
        # 漢字の混じるローマ字の文(「iPod 25周年 - Apple Music」のような日本語の見出し)は決めない
        return _latin_language(plain) if counts.han_chars == 0 else None
    return leader


def strip_quotes(text: str) -> str:
    """引用を空白に置き換える(前後の語がつながらないように空白にする)。

    「」『』“” は 1 回の走査で開きと閉じを対応づける(入れ子は外側ごと除く。本文の長さに比例する
    手間で済ませ、深い入れ子でも全文を何度も走査しない)。対応の取れない閉じは無視し、閉じの無い
    開きは中身ごと残す(閉じ忘れの括弧ひとつで本文の残りを捨てない)。" は同じ行の中だけで組にする。
    """
    out: list[str] = []
    opened: list[tuple[str, int]] = []  # (待っている閉じ, 開きを置いた out の位置)
    for ch in _ASCII_QUOTE_RE.sub(" ", text):
        if opened and ch == opened[-1][0]:
            del out[opened.pop()[1] :]
            out.append(" ")
        else:
            if ch in _QUOTE_PAIRS:
                opened.append((_QUOTE_PAIRS[ch], len(out)))
            out.append(ch)
    return "".join(out)


def language_mark(language: str | None) -> str | None:
    """配信とノートに出す元の言語の印(`🌐 英語`)。日本語・未判定(None)・知らない値なら None
    (印を出さない)。digest・Telegram・Items ノートが同じ形で出す(GH#439)。"""
    label = LANGUAGE_LABELS.get(language) if language is not None else None
    return f"{LANGUAGE_MARK} {label}" if label is not None else None


@dataclass
class _ScriptCounts:
    """文字の種類ごとの数。語 = 同じ種類の文字の連なり(looks_japanese の数え方)。"""

    kana_chars: int = 0
    ja_chars: int = 0  # かなと漢字
    han_chars: int = 0
    ja_words: int = 0  # かなと漢字の語
    other_words: int = 0  # それ以外の語(looks_japanese が日本語の語と比べる側)
    han_words: int = 0
    hangul_words: int = 0
    latin_words: int = 0
    rest_words: int = 0  # ハングル・ローマ字以外の、かな・漢字でない文字の語


_JA_SCRIPTS = frozenset({"hiragana", "katakana", "kanji"})


def _count_scripts(text: str) -> _ScriptCounts:
    """文字の種類ごとの数。

    ja_words・other_words は looks_japanese の数え方のまま(かな・漢字は種類ごと、それ以外は
    まとめて 1 つの種類として語に区切る。GH#173 の判定を変えないため)。hangul_words・
    latin_words・rest_words は、それ以外の文字を種類ごとに区切った語の数。
    """
    counts = _ScriptCounts()
    previous: str | None = None  # 細かい種類(hangul・latin・other を分ける)
    previous_coarse: str | None = None  # かな・漢字の種類と、それ以外をまとめた other
    for ch in text:
        script = _char_script(ch) if ch.isalpha() else None
        coarse = None if script is None else (script if script in _JA_SCRIPTS else "other")
        if coarse is not None and coarse != previous_coarse:
            if coarse == "other":
                counts.other_words += 1
            else:
                counts.ja_words += 1
        if script is not None and script != previous:
            if script == "kanji":
                counts.han_words += 1
            elif script == "hangul":
                counts.hangul_words += 1
            elif script == "latin":
                counts.latin_words += 1
            elif script == "other":
                counts.rest_words += 1
        if script in ("hiragana", "katakana"):
            counts.kana_chars += 1
        if script in _JA_SCRIPTS:
            counts.ja_chars += 1
        if script == "kanji":
            counts.han_chars += 1
        previous = script
        previous_coarse = coarse
    return counts


def _latin_language(text: str) -> str:
    """ローマ字の文が英語(en)か、ほかの言語(other)か。機能語の数で決める(同じなら英語)。"""
    english = other = 0
    for word in _LATIN_WORD_RE.findall(text):
        lowered = word.casefold()
        if lowered in _EN_WORDS:
            english += 1
        elif lowered in _OTHER_LATIN_WORDS:
            other += 1
    return "other" if other > english else "en"


def _char_script(ch: str) -> str:
    """文字の種類。looks_japanese はかな・漢字とそれ以外(hangul・latin・other)だけを見分ける。"""
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF or 0xFF66 <= code <= 0xFF9F:  # 全角・半角のカタカナ
        return "katakana"
    if code == 0x3005 or 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:  # 々・漢字
        return "kanji"
    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return "hangul"
    if code <= 0x024F or 0x1E00 <= code <= 0x1EFF or 0xFF21 <= code <= 0xFF5A:
        return "latin"  # ASCII・ラテン文字の拡張・全角の英字
    return "other"
