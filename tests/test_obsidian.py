import json
import logging
import sqlite3
from pathlib import Path

import pytest
import yaml

from app.config import Config, ObsidianConfig
from app.db import apply_migrations, connect
from app.export import ExportError, export_digest, export_pending_notes

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


def add_digest(conn, item_ids, *, date="2026-07-06", status="ok") -> int:
    return conn.execute(
        "INSERT INTO digests (digest_date, status, markdown, item_ids, item_count) "
        "VALUES (?, ?, '# ダイジェスト本文', ?, ?)",
        (date, status, json.dumps(item_ids), len(item_ids)),
    ).lastrowid


def read_front(path: Path) -> dict:
    text = path.read_text()
    return yaml.safe_load(text.split("---\n")[1])


def test_vault_unset_raises(conn, tmp_path) -> None:
    digest_id = add_digest(conn, [])
    with pytest.raises(ExportError, match="vault_path"):
        export_digest(conn, Config(config_dir=tmp_path), digest_id, logger)


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


def test_export_pending_notes_conflict_keeps_pending(conn, tmp_path) -> None:
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

    export_pending_notes(conn, config, logger)
    pending = conn.execute(
        "SELECT count(*) FROM item_notes WHERE exported_at IS NULL"
    ).fetchone()[0]
    assert pending == 1  # 未反映のまま(翌 run で再試行)
    conflict = note.with_suffix(".conflict.md")
    assert conflict.exists()
    assert "後で深掘りする" in conflict.read_text()  # 感想自体は退避側に保全
