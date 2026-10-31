import json
import logging
import os
import signal
import sqlite3
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import Config, ObsidianConfig
from app.db import apply_migrations, connect
from app.export import ExportError, export_digest, export_pending_notes
from app.export import obsidian as obsidian_module
from app.vault_dir import VaultPathError
from app.vault_guard import VaultGuard
from tests.test_vault_guard import (
    HANG_GUARD_S,
    gate_vault_threads,
    join_vault_threads,
    mark_blocked,
)

logger = logging.getLogger("test-obsidian")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed-a')")  # id=2
    conn.execute("INSERT INTO sources (type, name) VALUES ('x', 'home-tl')")  # id=3
    yield conn
    conn.close()


def make_config(tmp_path: Path) -> Config:
    return Config(
        config_dir=tmp_path, obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault"))
    )


def add_digested(
    conn, url, *, source_id=2, title="記事タイトル", title_ja=None,
    body="抽出済み本文。" * 100, score=80.0
):
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
        "VALUES (?, ?, ?, ?, 'sha256:x', 'digested')",
        (source_id, url, url, title),
    ).lastrowid
    conn.execute(
        "INSERT INTO documents (item_id, extracted_text, extraction_status, expires_at) "
        "VALUES (?, ?, 'ok', '2026-08-01T00:00:00Z')",
        (item_id, body),
    )
    conn.execute(
        "INSERT INTO analyses (item_id, title_ja, summary, category, relevance_score, novelty, "
        "negative_labels, recommendation_reason, implication, total_score, engine, model, "
        "prompt_version, input_hash) "
        "VALUES (?, ?, '要約文', 'カテゴリ', 80, 'new', '[]', '理由', '示唆', ?, 'codex-cli', "
        "'m', 'analyze-v001', ?)",
        (item_id, title_ja, score, f"sha256:{url}"),
    )
    return item_id


def add_digest(
    conn, item_ids, *, date="2026-07-06", status="ok", bookmark_item_ids=None
) -> int:
    return conn.execute(
        "INSERT INTO digests (digest_date, status, markdown, item_ids, item_count, "
        "bookmark_item_ids) VALUES (?, ?, '# ダイジェスト本文', ?, ?, ?)",
        (date, status, json.dumps(item_ids), len(item_ids),
         json.dumps(bookmark_item_ids) if bookmark_item_ids is not None else None),
    ).lastrowid


def read_front(path: Path) -> dict:
    text = path.read_text()
    return yaml.safe_load(text.split("---\n")[1])


def test_vault_unset_raises(conn, tmp_path) -> None:
    digest_id = add_digest(conn, [])
    with pytest.raises(ExportError, match="vault_path"):
        export_digest(conn, Config(config_dir=tmp_path), digest_id, logger)


@pytest.mark.parametrize("swapped", ["SignalForge", "Daily", "Items"])
def test_folder_symlink_outside_vault_is_never_written(conn, tmp_path, swapped) -> None:
    # 出力先のフォルダ(folder・Daily・Items)が vault の外への symlink なら、辿って外に書かず、
    # export を失敗にする。感想の追いパスも同じで、反映済みにしない(GH#204)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (digest_id, item),
    )
    add_note(conn, item, "記事への感想")
    add_digest_note(conn, digest_id, "日次の感想")
    config = make_config(tmp_path)
    base = tmp_path / "vault" / "SignalForge"
    link = base if swapped == "SignalForge" else base / swapped
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    for sub in ("Daily", "Items"):  # 外にも同じ形の置き場がある
        (outside / sub).mkdir(parents=True)
    os.symlink(outside if swapped == "SignalForge" else outside / swapped, link)

    with pytest.raises(VaultPathError, match=swapped):
        export_digest(conn, config, digest_id, logger)
    with pytest.raises(VaultPathError, match=swapped):
        export_pending_notes(conn, config, logger)

    assert [p for p in outside.rglob("*") if p.is_file()] == []
    pending_items = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    pending_digests = conn.execute(
        "SELECT count(*) FROM digest_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    # Daily だけが外を指すときは、Items のノートは vault の中に書けるので反映済みになる
    assert pending_items == (0 if swapped == "Daily" else 1)
    assert pending_digests == 1


def test_vault_path_symlink_and_nested_folder_are_written_inside(conn, tmp_path) -> None:
    # vault_path そのものは本人が設定した起点なので symlink(同期フォルダへのリンクなど)でよく、
    # folder が入れ子の相対パスでも、従来どおりその中に書く(GH#204 の境界は vault の実体から下)
    real = tmp_path / "real-vault"
    real.mkdir()
    (tmp_path / "vault-link").symlink_to(real)
    config = Config(
        config_dir=tmp_path,
        obsidian=ObsidianConfig(vault_path=str(tmp_path / "vault-link"), folder="Notes/SF"),
    )
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])

    export_digest(conn, config, digest_id, logger)

    base = real / "Notes" / "SF"
    notes = list((base / "Items").glob("*.md"))
    assert [p.name[:8] for p in notes] == ["20260706"]
    daily = (base / "Daily" / "2026-07-06.md").read_text()
    assert f"[[{notes[0].stem}]]" in daily and "# ダイジェスト本文" in daily
    assert "要約文" in notes[0].read_text()


def test_notes_created_with_naming_and_wikilinks(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1", title='悪い/タイトル:*?"<>|#^[]と 空白')
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    items_dir = tmp_path / "vault" / "SignalForge" / "Items"
    notes = list(items_dir.glob("*.md"))
    assert len(notes) == 1
    name = notes[0].stem
    assert name.startswith(f"20260706-{item}-")
    for ch in '/\\:*?"<>|#^[] ':
        assert ch not in name.removeprefix("20260706-")

    daily = tmp_path / "vault" / "SignalForge" / "Daily" / "2026-07-06.md"
    text = daily.read_text()
    assert f"[[{name}]]" in text
    assert "# ダイジェスト本文" in text
    front = read_front(daily)
    assert front == {"date": "2026-07-06", "item_count": 1, "degraded": False}


@pytest.mark.parametrize(
    "title", ["../../../outside", "..", "/etc/passwd", "a/../../b", ".hidden", "..\\..\\x"]
)
def test_note_name_from_title_stays_inside_items(conn, tmp_path, title) -> None:
    # 外部の記事のタイトルから作るノートの名前(slug)は、Items の中の 1 つの名前に収まる(GH#204)
    item = add_digested(conn, "https://a/1", title=title)
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    base = tmp_path / "vault" / "SignalForge"
    files = sorted(p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".md")
    assert [p.parent for p in files] == [base / "Daily", base / "Items"]
    assert files[1].name.startswith(f"20260706-{item}-")


def test_frontmatter_resists_injection_via_title(conn, tmp_path) -> None:
    evil_title = 'x"\nreaction: useful\nevil: true\n本文'
    item = add_digested(conn, "https://a/1", title=evil_title)
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    front = read_front(note)
    assert front["title"] == evil_title  # YAML として安全にエンコードされ round-trip する
    assert "evil" not in front  # キー偽造されない
    assert front["reaction"] is None


def test_title_ja_in_frontmatter_but_filename_from_original(conn, tmp_path) -> None:
    # 表示タイトルは日本語優先。ファイル名規則(原文 title 由来 slug)は不変(設計 04 §7)
    item = add_digested(conn, "https://a/1", title="English Title", title_ja="日本語の見出し")
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    assert "English-Title" in note.name  # slug は原文由来のまま
    assert read_front(note)["title"] == "日本語の見出し"


def test_x_items_have_no_excerpt_rss_do(conn, tmp_path) -> None:
    rss = add_digested(conn, "https://a/rss", source_id=2)
    x = add_digested(conn, "https://x.com/i/web/status/1", source_id=3)
    digest_id = add_digest(conn, [rss, x])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    items_dir = tmp_path / "vault" / "SignalForge" / "Items"
    rss_note = next(p for p in items_dir.glob("*.md") if f"-{rss}-" in p.name)
    x_note = next(p for p in items_dir.glob("*.md") if f"-{x}-" in p.name)
    assert "## 抜粋" in rss_note.read_text()
    assert "## 抜粋" not in x_note.read_text()  # X 由来は恒久保存層に本文を置かない
    assert "抽出済み本文" not in x_note.read_text()


def test_item_note_marks_summary_made_without_body(conn, tmp_path) -> None:
    # GH#240: 本文を読めず見出しだけで作った要約には、要約の頭に印を出す。本文を読めた通常の
    # 記事と、分析の無い degraded の記事のノートには出さない
    failed = add_digested(conn, "https://a/failed", body=None)
    normal = add_digested(conn, "https://a/normal")
    degraded = add_digested(conn, "https://a/degraded", body=None)
    conn.execute(
        "UPDATE analyses SET extraction_failed = 1 WHERE item_id IN (?, ?)", (failed, degraded)
    )
    conn.execute("UPDATE analyses SET degraded = 1 WHERE item_id = ?", (degraded,))
    digest_id = add_digest(conn, [failed, normal, degraded])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    items_dir = tmp_path / "vault" / "SignalForge" / "Items"

    def note_text(item_id: int) -> str:
        return next(p for p in items_dir.glob("*.md") if f"-{item_id}-" in p.name).read_text()

    assert "## 要約\n> ⚠️ 本文を読めていない(見出しだけで分析)\n\n要約文\n" in note_text(failed)
    assert "## 要約\n要約文\n" in note_text(normal)
    assert "本文を読めていない" not in note_text(normal)
    assert "本文を読めていない" not in note_text(degraded)


def test_regeneration_preserves_handwritten_and_user_frontmatter(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    # ユーザーが手書きメモと reaction を書く
    text = note.read_text()
    text = text.replace("reaction: null", "reaction: useful")
    note.write_text(text + "\n手書きのメモです。\n")

    conn.execute("UPDATE analyses SET summary = '更新された要約'")
    export_digest(conn, config, digest_id, logger)

    updated = note.read_text()
    assert "更新された要約" in updated  # GENERATED 内は置換
    assert "手書きのメモです。" in updated  # 手書き領域は保持
    assert read_front(note)["reaction"] == "useful"  # ユーザー編集キーは保持
    assert read_front(note)["score"] == 80  # 機械管理キーは更新対象


def test_missing_marker_writes_conflict_file_without_overwriting(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next(
        p for p in (tmp_path / "vault" / "SignalForge" / "Items").glob("*.md")
        if ".conflict" not in p.name
    )
    note.write_text("マーカーを消した手書きノート")
    export_digest(conn, config, digest_id, logger)

    assert note.read_text() == "マーカーを消した手書きノート"  # 上書きしない
    conflict = note.with_suffix(".conflict.md")
    assert conflict.exists()

    # 既存 conflict も上書きせず連番で退避する
    conflict_content = conflict.read_text()
    export_digest(conn, config, digest_id, logger)
    assert conflict.read_text() == conflict_content
    assert note.with_suffix(".conflict-2.md").exists()


def test_note_symlink_is_not_followed_and_goes_to_conflict(conn, tmp_path, caplog) -> None:
    # ノート自体が外への symlink なら、辿って読み書きせず、リンクも置き換えずに新版を
    # .conflict.md に退避する(マーカー欠損と同じ fail-safe。GH#204)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)
    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    outside = tmp_path / "outside.md"
    outside.write_text(note.read_text() + "\n外の手書き\n")  # 外のファイルもノートの形
    outside_text = outside.read_text()
    note.unlink()
    note.symlink_to(outside)
    conn.execute("UPDATE analyses SET summary = '更新された要約'")

    with caplog.at_level(logging.WARNING, logger="test-obsidian"):
        export_digest(conn, config, digest_id, logger)

    assert outside.read_text() == outside_text  # 外のファイルは書き換えない
    assert note.is_symlink()  # リンクも置き換えない
    conflict = note.with_suffix(".conflict.md")
    assert "更新された要約" in conflict.read_text()
    assert "外の手書き" not in conflict.read_text()  # 外の中身を読んで取り込まない
    assert "symlink" in caplog.text


def test_note_fifo_does_not_block_and_goes_to_conflict(conn, tmp_path) -> None:
    # ノートの名前に FIFO(名前付きパイプ)があっても、書き手を待って止まらない。通常のファイル
    # でないので触れずに新版を .conflict.md に退避する(GH#204)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)
    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    note.unlink()
    os.mkfifo(note)

    def stuck(signum, frame):  # 止まったら待ち続けずにテストを失敗させる
        raise TimeoutError("FIFO の open で止まった")

    previous = signal.signal(signal.SIGALRM, stuck)
    signal.alarm(5)
    try:
        export_digest(conn, config, digest_id, logger)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    assert stat.S_ISFIFO(note.lstat().st_mode)  # FIFO には触れない
    assert "要約文" in note.with_suffix(".conflict.md").read_text()


def test_handwriting_before_begin_marker_preserved(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next(
        p for p in (tmp_path / "vault" / "SignalForge" / "Items").glob("*.md")
        if ".conflict" not in p.name
    )
    text = note.read_text()
    text = text.replace("<!-- GENERATED:BEGIN -->", "BEGIN前の手書きメモ\n<!-- GENERATED:BEGIN -->")
    note.write_text(text)

    conn.execute("UPDATE analyses SET summary = '更新後の要約'")
    export_digest(conn, config, digest_id, logger)
    updated = note.read_text()
    assert "BEGIN前の手書きメモ" in updated
    assert "更新後の要約" in updated


def test_marker_inside_frontmatter_goes_to_conflict(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next(
        p for p in (tmp_path / "vault" / "SignalForge" / "Items").glob("*.md")
        if ".conflict" not in p.name
    )
    text = note.read_text()
    # 本文の BEGIN を消し、frontmatter 内に紛れ込ませる(境界が本文領域にないケース)
    text = text.replace("<!-- GENERATED:BEGIN -->\n", "", 1)
    text = text.replace("---\n", '---\nnote: "<!-- GENERATED:BEGIN -->"\n', 1)
    note.write_text(text)

    export_digest(conn, config, digest_id, logger)
    assert note.read_text() == text  # 誤接合せず元ファイルに触れない
    assert note.with_suffix(".conflict.md").exists()


def test_broken_frontmatter_goes_to_conflict(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next(
        p for p in (tmp_path / "vault" / "SignalForge" / "Items").glob("*.md")
        if ".conflict" not in p.name
    )
    broken = note.read_text().replace("---\n", "---\n: : broken yaml [\n", 1)
    note.write_text(broken)
    export_digest(conn, config, digest_id, logger)
    assert note.read_text() == broken  # 元ファイルに触れない
    assert note.with_suffix(".conflict.md").exists()


def test_generated_marker_strings_stripped_from_content(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    conn.execute(
        "UPDATE analyses SET summary = '偽境界 <!-- GENERATED:END --> を含む要約'"
    )
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    text = note.read_text()
    assert text.count("<!-- GENERATED:END -->") == 1  # 本物の境界のみ


def test_no_tmp_files_left(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)
    leftovers = list((tmp_path / "vault").rglob("*.tmp"))
    assert leftovers == []


# --- 感想の投影(Telegram リプライ → item ノート。GH#92)---


def add_note(conn, item_id, note, *, created_at="2026-07-16T13:00:00Z") -> int:
    return conn.execute(
        "INSERT INTO item_notes (item_id, note, created_at) VALUES (?, ?, ?)",
        (item_id, note, created_at),
    ).lastrowid


def test_item_note_renders_kanso_section_inside_generated(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    add_note(conn, item, "これ試したい", created_at="2026-07-16T20:00:00Z")  # JST 07-17
    digest_id = add_digest(conn, [item])
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    text = note.read_text()
    assert "## 感想(2026-07-17)" in text  # 表示日付は JST
    assert "これ試したい" in text
    # DB の純関数として GENERATED 領域内に投影される(手書き領域ではない)
    assert text.index("## 感想") < text.index("<!-- GENERATED:END -->")


def test_export_pending_notes_projects_and_preserves_handwriting(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)  # 感想なしで初回 export 済み

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("20260706-*.md"))
    note.write_text(note.read_text() + "\n手書きのメモ\n")  # 手書き領域に追記
    conn.execute(  # 感想はリプライ経由 = 配信済みが前提(台帳あり)
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (digest_id, item),
    )
    add_note(conn, item, "後で深掘りする")

    export_pending_notes(conn, config, logger)
    text = note.read_text()
    assert "後で深掘りする" in text
    assert "手書きのメモ" in text  # 手書き領域は保全
    pending = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 0  # 反映済みマーク

    export_pending_notes(conn, config, logger)  # 冪等: 2回目は対象なし・変更なし
    assert note.read_text() == text


def test_export_pending_notes_vault_unset_raises(conn, tmp_path) -> None:
    with pytest.raises(ExportError, match="vault_path"):
        export_pending_notes(conn, Config(config_dir=tmp_path), logger)


def test_export_pending_notes_conflict_keeps_pending(conn, tmp_path, caplog) -> None:
    # conflict 退避 = 正本ノート未更新なので exported_at を立てない(Codex must)。
    # マーカー修復後の翌 run で自動再投影される
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("20260706-*.md"))
    note.write_text(note.read_text().replace("<!-- GENERATED:END -->", ""))  # マーカー破壊
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (digest_id, item),
    )
    add_note(conn, item, "後で深掘りする")

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="test-obsidian"):
        export_pending_notes(conn, config, logger)
    assert "1 件を .conflict.md に退避" in caplog.text  # conflicts カウントと WARN が残る
    pending = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 1  # 未反映のまま(翌 run で再試行)
    conflict = note.with_suffix(".conflict.md")
    assert conflict.exists()
    assert "後で深掘りする" in conflict.read_text()  # 感想自体は退避側に保全


def add_digest_note(conn, digest_id, note, *, created_at="2026-07-17T13:00:00Z") -> int:
    return conn.execute(
        "INSERT INTO digest_notes (digest_id, note, created_at) VALUES (?, ?, ?)",
        (digest_id, note, created_at),
    ).lastrowid


def test_daily_note_renders_digest_kanso_inside_generated(conn, tmp_path) -> None:
    # GH#96: 日次感想は Daily ノートの GENERATED 領域に投影される
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    add_digest_note(conn, digest_id, "全体的にツール寄りで良い", created_at="2026-07-17T20:00:00Z")
    export_digest(conn, make_config(tmp_path), digest_id, logger)

    daily = tmp_path / "vault" / "SignalForge" / "Daily" / "2026-07-06.md"
    text = daily.read_text()
    assert "## 感想(2026-07-18)" in text  # JST 日付
    assert "全体的にツール寄りで良い" in text
    assert text.index("## 感想") < text.index("<!-- GENERATED:END -->")
    assert "[[" in text  # wikilink(既存内容)も維持


def test_export_pending_digest_notes_projects_daily(conn, tmp_path) -> None:
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)  # 感想なしで初回 export 済み

    daily = tmp_path / "vault" / "SignalForge" / "Daily" / "2026-07-06.md"
    daily.write_text(daily.read_text() + "\n手書きの日記\n")
    add_digest_note(conn, digest_id, "翌日に書いた感想")

    export_pending_notes(conn, config, logger)
    text = daily.read_text()
    assert "翌日に書いた感想" in text
    assert "手書きの日記" in text  # 手書き領域は保全
    assert "[[" in text  # wikilink を再計算して維持
    pending = conn.execute(
        "SELECT count(*) FROM digest_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 0

    export_pending_notes(conn, config, logger)  # 冪等
    assert daily.read_text() == text


def test_export_pending_digest_notes_conflict_keeps_pending(conn, tmp_path) -> None:
    # GH#96(Codex should): Daily の conflict 退避時も exported_at を立てない(#93 と同契約)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    daily = tmp_path / "vault" / "SignalForge" / "Daily" / "2026-07-06.md"
    daily.write_text(daily.read_text().replace("<!-- GENERATED:END -->", ""))  # マーカー破壊
    add_digest_note(conn, digest_id, "翌日に書いた感想")

    export_pending_notes(conn, config, logger)
    pending = conn.execute(
        "SELECT count(*) FROM digest_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 1  # 未反映のまま(翌 run で再試行)
    conflict = daily.with_suffix(".conflict.md")
    assert conflict.exists()
    assert "翌日に書いた感想" in conflict.read_text()  # 感想自体は退避側に保全


def test_bookmark_items_exported_as_notes_and_wikilinks(conn, tmp_path) -> None:
    # 🔖 保存節の item(GH#99)も Items ノート化され Daily の wikilink に載る
    normal = add_digested(conn, "https://a/1", title="通常記事")
    bm = add_digested(conn, "https://b/1", title="ブクマ記事", title_ja="保存の見出し")
    digest_id = add_digest(conn, [normal], bookmark_item_ids=[bm])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)

    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    notes = sorted(p.name for p in (base / "Items").glob("*.md"))
    assert len(notes) == 2
    assert any("ブクマ記事" in name for name in notes)
    daily = (base / "Daily" / "2026-07-06.md").read_text()
    assert sum(1 for line in daily.splitlines() if line.startswith("[[")) == 2
    front = read_front(base / "Items" / next(n for n in notes if "ブクマ記事" in n))
    assert front["title"] == "保存の見出し"


def test_relisted_in_later_bookmark_section_reuses_first_note(conn, tmp_path) -> None:
    # GH#242: 配信済みの記事が後日の 🔖 保存節に再掲されても、別日付の Items ノートを作らず
    # 最初のノートを使う。再掲した日の Daily のリンクと、その後の感想の投影も最初のノートへ
    item = add_digested(conn, "https://a/1", title="配信済み記事")
    first_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, first_id, logger)
    conn.execute(  # 初日に記事メッセージとして配信済み(感想のリプライ先)
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (first_id, item),
    )

    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    first_note = next((base / "Items").glob(f"20260706-{item}-*.md"))
    first_note.write_text(first_note.read_text() + "\n初日の手書きメモ\n")

    other = add_digested(conn, "https://a/2", title="当日の記事")
    later_id = add_digest(conn, [other], date="2026-07-10", bookmark_item_ids=[item])
    export_digest(conn, config, later_id, logger)

    assert list((base / "Items").glob(f"*-{item}-*.md")) == [first_note]  # 別日付を作らない
    later_daily = base / "Daily" / "2026-07-10.md"
    assert f"[[{first_note.stem}]]" in later_daily.read_text()
    text = first_note.read_text()
    assert "初日の手書きメモ" in text  # 手書き領域は保全
    assert read_front(first_note)["digest_date"] == "2026-07-06"  # ノート名の日付のまま

    add_note(conn, item, "再掲の後に書いた感想")
    add_digest_note(conn, later_id, "再掲の日の日次感想")
    export_pending_notes(conn, config, logger)
    assert "再掲の後に書いた感想" in first_note.read_text()
    assert "初日の手書きメモ" in first_note.read_text()
    assert list((base / "Items").glob(f"*-{item}-*.md")) == [first_note]
    later_text = later_daily.read_text()
    assert "再掲の日の日次感想" in later_text
    assert f"[[{first_note.stem}]]" in later_text  # 追いパスの再計算でも最初のノートへ


def test_item_dropped_by_force_and_delivered_later_reuses_first_note(conn, tmp_path) -> None:
    # GH#242: --force の置換で外れた item が後日また配信されても、初日に export したノートを使う。
    # 後日の Daily のリンクと、後日の配信へのリプライの感想の投影も最初のノートへ
    item = add_digested(conn, "https://a/1", title="外れた記事")
    first_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, first_id, logger)
    # 同じ日の --force の置換で台帳(item_ids)から外れ、候補(analyzed)に戻った状態(builder と同じ)
    other = add_digested(conn, "https://a/2", title="差し替えた記事")
    conn.execute("UPDATE digests SET item_ids = ? WHERE id = ?", (json.dumps([other]), first_id))
    conn.execute("UPDATE items SET status = 'analyzed' WHERE id = ?", (item,))

    later_id = add_digest(conn, [item], date="2026-07-08")
    export_digest(conn, config, later_id, logger)
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 501)",
        (later_id, item),
    )
    add_note(conn, item, "後日の配信へのリプライ")
    export_pending_notes(conn, config, logger)

    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    notes = list((base / "Items").glob(f"*-{item}-*.md"))
    assert [p.name[:8] for p in notes] == ["20260706"]  # 別日付を作らない
    assert f"[[{notes[0].stem}]]" in (base / "Daily" / "2026-07-08.md").read_text()
    assert "後日の配信へのリプライ" in notes[0].read_text()
    # 追いパスで書き直しても、frontmatter の日付は配信の日ではなくノート名の日付
    assert read_front(notes[0])["digest_date"] == "2026-07-06"


def test_notes_on_item_dropped_by_force_project_to_first_note(conn, tmp_path) -> None:
    # GH#190: --force の置換で外れた記事にも、置換の前のメッセージへの感想を投影する。台帳の
    # 前の世代の行が残るので投影先を解決でき、置換の前に export した Items ノートへ書く(GH#242)
    from dataclasses import replace

    from app.config import DigestConfig
    from app.digest import generate_digest
    from tests.test_ranking import add_analyzed

    kept = add_analyzed(conn, "https://a/kept", relevance=90)
    dropped = add_analyzed(conn, "https://a/dropped", relevance=80)
    config = replace(make_config(tmp_path), digest=DigestConfig(max_items=2))
    digest_id = generate_digest(conn, config, logger)
    export_digest(conn, config, digest_id, logger)
    for item_id, message_id in ((None, 500), (kept, 501), (dropped, 502)):  # 配信済み
        conn.execute(
            "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, ?)",
            (digest_id, item_id, message_id),
        )
    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    first_note = next((base / "Items").glob(f"*-{dropped}-*.md"))

    narrowed = replace(config, digest=DigestConfig(max_items=1))
    assert generate_digest(conn, narrowed, logger, force=True) == digest_id
    row = conn.execute("SELECT item_ids FROM digests").fetchone()
    assert json.loads(row["item_ids"]) == [kept]  # dropped は再選抜で外れた
    export_digest(conn, narrowed, digest_id, logger)
    add_note(conn, dropped, "外れる前のメッセージへの感想")  # 502 へのリプライ
    export_pending_notes(conn, narrowed, logger)

    assert "外れる前のメッセージへの感想" in first_note.read_text()
    assert list((base / "Items").glob(f"*-{dropped}-*.md")) == [first_note]  # 別のノートを作らない
    pending = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 0  # 反映済み


def test_existing_duplicate_notes_are_left_in_place(conn, tmp_path) -> None:
    # GH#242: 修正前に再掲で作られた別日付のノート(重複)は動かさず統合もしない。その日を
    # export し直すと従来どおり重複のノートを書き直し、感想の追いパスは最初のノートへ
    item = add_digested(conn, "https://a/1", title="重複の記事")
    first_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, first_id, logger)
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (first_id, item),
    )
    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    first_note = next((base / "Items").glob(f"20260706-{item}-*.md"))
    duplicate = first_note.with_name(first_note.name.replace("20260706", "20260710", 1))
    duplicate.write_text(first_note.read_text() + "\n重複の側の手書き\n")
    first_note.write_text(first_note.read_text() + "\n最初の側の手書き\n")
    later_id = add_digest(conn, [], date="2026-07-10", bookmark_item_ids=[item])

    export_digest(conn, config, later_id, logger)  # 再掲の日を export し直す
    add_note(conn, item, "後で書いた感想")
    export_pending_notes(conn, config, logger)

    names = sorted(p.name for p in (base / "Items").glob(f"*-{item}-*.md"))
    assert names == sorted([first_note.name, duplicate.name])
    assert f"[[{duplicate.stem}]]" in (base / "Daily" / "2026-07-10.md").read_text()
    assert "重複の側の手書き" in duplicate.read_text()
    assert "最初の側の手書き" in first_note.read_text()
    assert "後で書いた感想" in first_note.read_text()


def test_conflict_file_is_not_reused_as_item_note(conn, tmp_path) -> None:
    # GH#242: 既存のノートを探すとき、退避した .conflict は出力先にしない
    item = add_digested(conn, "https://a/1", title="退避の記事")
    first_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, first_id, logger)
    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    first_note = next((base / "Items").glob(f"20260706-{item}-*.md"))
    conflict = first_note.with_suffix(".conflict.md")
    first_note.rename(conflict)  # 本人が元のノートを片付け、退避だけが残った
    conflict_text = conflict.read_text()

    later_id = add_digest(conn, [], date="2026-07-10", bookmark_item_ids=[item])
    export_digest(conn, config, later_id, logger)

    assert conflict.read_text() == conflict_text  # 退避には書かない
    assert list((base / "Items").glob(f"20260710-{item}-*.md"))  # 従来の規則で作る


@pytest.mark.parametrize("title", ["sync.conflict", "sync.conflict-2"])
def test_title_ending_with_conflict_still_reuses_first_note(conn, tmp_path, title) -> None:
    # GH#242(Codex must): タイトル自体が .conflict で終わるノートも、退避と取り違えずに使う
    item = add_digested(conn, "https://a/1", title=title)
    first_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, first_id, logger)
    later_id = add_digest(conn, [], date="2026-07-10", bookmark_item_ids=[item])
    export_digest(conn, config, later_id, logger)

    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    notes = list((base / "Items").glob(f"*-{item}-*.md"))
    assert [p.name for p in notes] == [f"20260706-{item}-{title}.md"]
    assert f"[[20260706-{item}-{title}]]" in (base / "Daily" / "2026-07-10.md").read_text()


def test_pending_pass_links_daily_to_note_it_just_created(conn, tmp_path) -> None:
    # GH#242: Items ノートがまだ無い item(export が失敗した日など)を追いパスが作ったとき、
    # 同じ追いパスで書き直す再掲の日の Daily も、そのノートへリンクする
    item = add_digested(conn, "https://a/1", title="未出力の記事")
    first_id = add_digest(conn, [item], date="2026-07-06")
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (first_id, item),
    )
    later_id = add_digest(conn, [], date="2026-07-10", bookmark_item_ids=[item])
    add_note(conn, item, "記事への感想")
    add_digest_note(conn, later_id, "再掲の日の日次感想")
    config = make_config(tmp_path)

    export_pending_notes(conn, config, logger)
    base = Path(config.obsidian.vault_path) / config.obsidian.folder
    notes = list((base / "Items").glob(f"*-{item}-*.md"))
    assert [p.name[:8] for p in notes] == ["20260706"]
    assert f"[[{notes[0].stem}]]" in (base / "Daily" / "2026-07-10.md").read_text()


# --- 読み取り後の変更の検出(GH#189)---


def hook_before_write(monkeypatch, note: Path, on_write) -> list[int]:
    """note の atomic_write の直前(= ノートの読み取り後)に on_write(回数) を差し込む。

    本人や同期ツールが、export がノートを読んだあと・書く前に保存した状況を再現する。
    """
    original = obsidian_module.atomic_write
    calls: list[int] = []

    def hooked(dir_fd, name, content, **kwargs):
        if name == note.name:
            calls.append(len(calls) + 1)
            on_write(len(calls))
        return original(dir_fd, name, content, **kwargs)

    monkeypatch.setattr(obsidian_module, "atomic_write", hooked)
    return calls


def test_regeneration_without_change_after_read_writes_once(
    conn, tmp_path, monkeypatch, caplog
) -> None:
    # 通常のケース: 読み取り後に変更が無ければ 1 回で書き、退避も WARN も出さない
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)
    items_dir = tmp_path / "vault" / "SignalForge" / "Items"
    note = next(items_dir.glob("*.md"))
    note.write_text(note.read_text() + "\n手書きのメモ\n")

    calls = hook_before_write(monkeypatch, note, lambda n: None)
    conn.execute("UPDATE analyses SET summary = '更新された要約'")
    with caplog.at_level(logging.WARNING, logger="test-obsidian"):
        export_digest(conn, config, digest_id, logger)

    text = note.read_text()
    assert "更新された要約" in text
    assert "手書きのメモ" in text
    assert calls == [1]  # やり直しなし
    assert list(items_dir.glob("*.conflict*.md")) == []
    assert "退避" not in caplog.text


def test_note_saved_after_read_is_reread_not_overwritten(conn, tmp_path, monkeypatch) -> None:
    # 読み取り後・書き込みの直前に本人が保存した → 上書きせず読み直して 1 回だけやり直す
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)
    items_dir = tmp_path / "vault" / "SignalForge" / "Items"
    note = next(items_dir.glob("*.md"))
    note.write_text(note.read_text() + "\n手書きのメモ\n")

    def save_once(n: int) -> None:
        if n == 1:
            note.write_text(note.read_text() + "\n直前に保存したメモ\n")

    calls = hook_before_write(monkeypatch, note, save_once)
    conn.execute("UPDATE analyses SET summary = '更新された要約'")
    export_digest(conn, config, digest_id, logger)

    text = note.read_text()
    assert "直前に保存したメモ" in text  # 後から保存した手書きを古い内容で上書きしない
    assert "手書きのメモ" in text
    assert "更新された要約" in text  # やり直しで GENERATED は更新される
    assert calls == [1, 2]
    assert list(items_dir.glob("*.conflict*.md")) == []
    assert list((tmp_path / "vault").rglob("*.tmp")) == []


def test_note_keeps_changing_goes_to_conflict_and_keeps_pending(
    conn, tmp_path, monkeypatch, caplog
) -> None:
    # やり直しでも変わり続ける → 正本ノートに触れず新版を .conflict.md に退避して WARN。
    # 未更新の扱いなので感想の exported_at は立てない(翌 run で再試行。マーカー欠損と同じ契約)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item], date="2026-07-06")
    config = make_config(tmp_path)
    export_digest(conn, config, digest_id, logger)
    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("20260706-*.md"))
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, 500)",
        (digest_id, item),
    )
    add_note(conn, item, "後で深掘りする")

    def save_every_time(n: int) -> None:
        note.write_text(note.read_text() + f"\n{n} 回目の保存\n")

    calls = hook_before_write(monkeypatch, note, save_every_time)
    with caplog.at_level(logging.WARNING, logger="test-obsidian"):
        export_pending_notes(conn, config, logger)

    saved = note.read_text()
    assert "1 回目の保存" in saved
    assert "2 回目の保存" in saved  # 本人の保存は上書きされない
    assert "後で深掘りする" not in saved  # 正本ノートは未更新
    assert calls == [1, 2]  # 読み直しての やり直しは 1 回だけ
    conflict = note.with_suffix(".conflict.md")
    assert "後で深掘りする" in conflict.read_text()  # 新版は退避側に保全
    assert f"読み取り後にノートが変更され続けたため、新版を .conflict に退避: {note.name}" in (
        caplog.text
    )
    assert "1 件を .conflict.md に退避" in caplog.text
    pending = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 1
    assert list((tmp_path / "vault").rglob("*.tmp")) == []


def test_new_note_created_before_write_is_not_overwritten(conn, tmp_path, monkeypatch) -> None:
    # 新規作成の途中(無いと読んだあと・rename の前)に同期でノートが届いた → 上書きしない。
    # 読み直すとマーカーが無いので、既存の fail-safe どおり .conflict.md に退避する
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    note = tmp_path / "vault" / "SignalForge" / "Items" / f"20260706-{item}-記事タイトル.md"

    def arrive_once(n: int) -> None:
        if n == 1:
            note.write_text("同期で先に届いた手書きノート")

    calls = hook_before_write(monkeypatch, note, arrive_once)
    export_digest(conn, config, digest_id, logger)

    assert calls == [1]
    assert note.read_text() == "同期で先に届いた手書きノート"
    assert note.with_suffix(".conflict.md").exists()


# --- vault が応答しないときの上限と、遅れた書き込みのロック(GH#341) ---


def block_first_write(monkeypatch) -> SimpleNamespace:
    """最初のノートの書き出しを、解除まで止める(vault の応答待ちの代わり)。

    止まった位置に着いたら reached が立つ。VaultGuard は上限の時間でなく、ここに着いたのを
    見てから見切る(gate_vault_threads。順序を時間に頼らない)。解除(release)を忘れたときは
    HANG_GUARD_S で進め、timed_out に残す(テストの誤り)。
    """
    gate_vault_threads(monkeypatch)
    blocker = SimpleNamespace(
        reached=threading.Event(), release=threading.Event(), calls=[], timed_out=[]
    )
    original = obsidian_module._write_note

    def blocking(directory, name, *args, **kwargs):
        blocker.calls.append(name)
        if len(blocker.calls) == 1:
            blocker.reached.set()
            mark_blocked()
            if not blocker.release.wait(HANG_GUARD_S):
                blocker.timed_out.append(True)
        return original(directory, name, *args, **kwargs)

    monkeypatch.setattr(obsidian_module, "_write_note", blocking)
    return blocker


def finish_blocked_write(blocker: SimpleNamespace) -> list[threading.Thread]:
    """止めた書き出しを解除し、vault-io のスレッドの終わりを待つ(終わらなかったものを返す)。"""
    blocker.release.set()
    return join_vault_threads()


def add_replied_items(conn, n: int) -> tuple[int, list[int]]:
    """記事メッセージとして送り、感想が付いた item を n 件作る(追いパスの対象)。"""
    items = [add_digested(conn, f"https://a/{i}", title=f"記事{i}") for i in range(n)]
    digest_id = add_digest(conn, items)
    for i, item in enumerate(items):
        conn.execute(
            "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, ?)",
            (digest_id, item, 500 + i),
        )
        add_note(conn, item, f"記事{i}への感想")
    conn.commit()
    return digest_id, items


def pending_item_notes(conn) -> int:
    return conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]


def test_export_stall_gives_up_and_keeps_notes_pending(conn, tmp_path, monkeypatch) -> None:
    # vault が応答しない(macOS のプライバシーの確認待ちなど)ときは見切り、
    # export を失敗にして先へ進む。止まった後は残りのノートで vault に触れず、感想を反映済みに
    # しない(次の実行で書く。GH#341)
    _digest_id, items = add_replied_items(conn, 2)
    config = make_config(tmp_path)
    blocker = block_first_write(monkeypatch)
    try:
        with pytest.raises(ExportError, match="vault が応答しない"):
            export_pending_notes(conn, config, logger, vault=VaultGuard())
        assert blocker.reached.is_set()
        assert len(blocker.calls) == 1  # 止まった後は、残りのノートで vault に触れない
        assert pending_item_notes(conn) == 2
    finally:
        alive = finish_blocked_write(blocker)
    assert alive == [] and blocker.timed_out == []

    # 次の実行は、止まったぶんを含めて全部を書き、反映済みにする
    export_pending_notes(conn, config, logger)
    assert pending_item_notes(conn) == 0
    texts = [
        note.read_text()
        for note in sorted((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    ]
    assert len(texts) == 2
    assert "記事0への感想" in texts[0] and "記事1への感想" in texts[1]


def test_export_digest_stall_raises_export_error(conn, tmp_path, monkeypatch) -> None:
    # その日の digest の export も同じ上限で見切る(Daily は同じ日の再走で書く。GH#341)
    item = add_digested(conn, "https://a/1")
    digest_id = add_digest(conn, [item])
    config = make_config(tmp_path)
    blocker = block_first_write(monkeypatch)
    try:
        with pytest.raises(ExportError, match="vault が応答しない"):
            export_digest(conn, config, digest_id, logger, vault=VaultGuard())
        assert blocker.calls == [f"20260706-{item}-記事タイトル.md"]  # Daily には進まない
    finally:
        alive = finish_blocked_write(blocker)
    assert alive == [] and blocker.timed_out == []

    export_digest(conn, config, digest_id, logger)
    assert (tmp_path / "vault" / "SignalForge" / "Daily" / "2026-07-06.md").exists()


def test_next_run_does_not_export_while_late_write_pending(
    conn, tmp_path, monkeypatch
) -> None:
    # 見切ったスレッドは後から古い内容で書き終えうる。それが終わるまで別の実行は export しない
    # (新しい感想を投影して反映済みにしたノートを、遅れた書き込みで置き換えさせない。GH#341)
    monkeypatch.setattr(obsidian_module, "_EXPORT_LOCK_WAIT_S", 0.05)
    _digest_id, (item,) = add_replied_items(conn, 1)
    config = make_config(tmp_path)
    blocker = block_first_write(monkeypatch)
    try:
        with pytest.raises(ExportError, match="vault が応答しない"):
            export_pending_notes(conn, config, logger, vault=VaultGuard())
        assert blocker.reached.is_set()
        add_note(conn, item, "後から届いた感想", created_at="2026-07-16T14:00:00Z")
        conn.commit()
        with pytest.raises(ExportError, match="別の実行の export が終わっていない"):
            export_pending_notes(conn, config, logger)
        assert pending_item_notes(conn) == 2
    finally:
        alive = finish_blocked_write(blocker)  # 遅れた書き込み(後の感想を含まない)が終わる
    assert alive == [] and blocker.timed_out == []

    # 止まったスレッドが書き終えたら、次の実行は後の感想も含めて書き、反映済みにする
    export_pending_notes(conn, config, logger)
    note = next((tmp_path / "vault" / "SignalForge" / "Items").glob("*.md"))
    text = note.read_text()
    assert "記事0への感想" in text and "後から届いた感想" in text
    assert pending_item_notes(conn) == 0


def test_vault_io_runs_off_the_db_thread(conn, tmp_path, monkeypatch) -> None:
    # vault のアクセスは vault-io のスレッドで、DB の読み書きは呼び出し側のスレッドで行う
    # (見切ったスレッドが後から DB に触れない。GH#341)
    gate_vault_threads(monkeypatch)
    _digest_id, _items = add_replied_items(conn, 1)
    config = make_config(tmp_path)
    writers: list[str] = []
    original = obsidian_module._write_note

    def recording(*args, **kwargs):
        writers.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(obsidian_module, "_write_note", recording)
    export_pending_notes(conn, config, logger)
    assert writers == ["vault-io"]
    assert pending_item_notes(conn) == 0
