"""元の記事の言語の判定(GH#439)。looks_japanese の結果は tests/test_deepdive.py が持つ(GH#173)。"""

import pytest

from app.language import LANGUAGE_LABELS, LANGUAGES, detect_language, language_mark

EN = (
    "The team released a new version of the tool. It is faster and uses less memory than the "
    "previous release, and the changes are described in the notes."
)
DE = (
    "Nach der Störung bei dem Anbieter ist der Dienst wieder erreichbar. Die Ursache ist "
    "nicht bekannt und wird mit dem Team untersucht."
)
FR = (
    "Les crédits sont épuisés plus vite que prévu et la plupart des utilisateurs le "
    "constatent dans les journaux du service."
)
PT = "Abrir um projeto no editor pode executar código de um invasor antes de você aprovar."
ZH = (
    "我每天早上找选题最烦的一件事，就是搜出来的全是旧闻。"
    "模型都换了两代了，搜出来的文章还在讲上个季度的东西。"
)
KO = "아침 7시 AI 브리핑 지난 밤 소식 5건 자바 취약점을 찾았습니다"
AR = "تخيّل أن يلعب البرنامج اللعبة بدلًا منك وترجع تلاقيه خلّص المهام"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("新しい版を公開しました。速くなり、メモリも減りました。", "ja"),
        (EN, "en"),
        # 機能語の無い短い英語の見出しは英語(ローマ字の見出しの多くは英語の記事)
        ("Unsloth Qwen3.8-27B Quantizations – Dynamic v3.1 Update", "en"),
        (DE, "other"),
        (FR, "other"),
        (PT, "other"),
        (ZH, "zh"),
        (KO, "ko"),
        (AR, "other"),
        # かなが 1 字でもあれば外国語にしない(英語を長く引く日本語の記事・X の画面の文字に
        # 埋もれた日本語の投稿)
        (
            "AI開発企業がツールを公開しました。 Our mission is to build the tools that "
            "teams use every day, and we are excited to share the beta with you.",
            None,
        ),
        ("Post Log in Sign up Show translation うふ 1:26 PM · Nov 16, 2026 Views", None),
        # 漢字だけの短い日本語の見出しは、中国語と決めない
        ("富士通、新製品発表", None),
        # 漢字の混じるローマ字の見出し(日本語のページ)は英語と決めない
        ("iPod 25周年 - Apple Music", None),
        # URL の部分(au・es)はほかの言語の機能語として数えない
        ("https://www.abc.net.au/news/es/di/la Comments", "en"),
        # 1 字の語(1Y の Y)はスペイン語の y と数えない
        ("CVE Tracker Loading Resolution Day Week Month Year Range 30D 1Y 5Y View", "en"),
        # 引用の中は数えない(日本語の文を引く英語の記事は英語)
        (EN + "「これは日本語の引用です」", "en"),
        # 文字の種類が拮抗するものは決めない
        ("Claude 设定", None),
        ("%PDF-1.7 obj stream the and of", None),
        ("2026-10-23 12:00", None),
        ("   ", None),
        ("", None),
        (None, None),
    ],
    ids=[
        "ja", "en", "en-title", "de", "fr", "pt", "zh", "ko", "ar", "ja-quoting-en",
        "ja-in-x-chrome", "kanji-only-short", "latin-with-kanji", "url-parts", "one-letter",
        "en-quoting-ja", "tie", "pdf", "no-letters", "blank", "empty", "none",
    ],
)
def test_detect_language(text, expected) -> None:
    assert detect_language(text) == expected


def test_detected_values_are_the_stored_ones() -> None:
    # 判定が返す値は analyses.source_language の CHECK の値と同じ(migration 043)
    assert set(LANGUAGES) == {"ja", "en", "zh", "ko", "other"}
    assert set(LANGUAGE_LABELS) == set(LANGUAGES) - {"ja"}


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "🌐 英語"),
        ("zh", "🌐 中国語"),
        ("ko", "🌐 韓国語"),
        ("other", "🌐 外国語"),
        ("ja", None),  # 日本語の記事には出さない
        (None, None),  # 未判定・043 より前の分析
        ("xx", None),  # 知らない値
    ],
)
def test_language_mark(language, expected) -> None:
    assert language_mark(language) == expected
