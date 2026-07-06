"""Obsidian export — Vault への蓄積層(設計 04 §7)。

- Daily/(digest 全文+wikilink)と Items/(1 item 1 ノート)を出力する
- frontmatter は YAML シリアライザで生成(外部由来文字列による構造破壊・キー偽造の防止)
- 再生成は GENERATED マーカー内のみ置換。手書きメモ・ユーザー変更 frontmatter キーは
  保持し、マーカー欠損時は上書きせず .conflict.md を並置(fail-safe)
- 書込は一時ファイル→rename のアトミック書き(同期ツールとの競合対策)
- X 由来は抜粋を出力しない(恒久保存層に本文を置かない。Issue #2 の確定ポリシー)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import yaml

from app.config import Config

GENERATED_BEGIN = "<!-- GENERATED:BEGIN -->"
GENERATED_END = "<!-- GENERATED:END -->"

EXCERPT_MAX_CHARS = 300  # RSS/manual の抜粋の長さ(恒久保存は要約が正本で、抜粋は補助)

_FORBIDDEN_IN_SLUG = re.compile(r'[/\\:*?"<>|#^\[\]\s]+')
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# ユーザー所有キー: 生成側は初期値を置くだけで、再生成時に上書きしない(設計 04 §7)
_USER_OWNED_KEYS = ("reaction",)


class ExportError(Exception):
    """Vault 未設定・書込不能。watchdog 通知対象(設計 04 §8)。"""


def export_digest(
    conn: sqlite3.Connection,
    config: Config,
    digest_id: int,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
) -> None:
    if config.obsidian.vault_path is None:
        raise ExportError("[obsidian].vault_path が未設定(宿題 H4)。export できない")
    base = Path(config.obsidian.vault_path).expanduser() / config.obsidian.folder
    daily_dir = base / "Daily"
    items_dir = base / "Items"
    daily_dir.mkdir(parents=True, exist_ok=True)
    items_dir.mkdir(parents=True, exist_ok=True)

    digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if digest is None:
        raise ExportError(f"digests.id={digest_id} がない")
    item_ids = json.loads(digest["item_ids"])
    rows = _load_items(conn, item_ids) if item_ids else []

    wikilinks = []
    conflicts = 0
    for row in rows:
        note_name = _item_note_name(digest["digest_date"], row["item_id"], row["title"])
        wikilinks.append(f"[[{note_name}]]")
        if not _write_note(
            items_dir / f"{note_name}.md",
            _item_frontmatter(row, digest["digest_date"]),
            _item_generated_body(row),
        ):
            conflicts += 1

    daily_name = digest["digest_date"]
    daily_front = {
        "date": digest["digest_date"],
        "item_count": digest["item_count"],
        "degraded": digest["status"] == "degraded",
    }
    links_section = "\n\n## ノート\n" + "\n".join(wikilinks) if wikilinks else ""
    daily_body = digest["markdown"] + links_section
    if not _write_note(daily_dir / f"{daily_name}.md", daily_front, daily_body):
        conflicts += 1

    if conflicts:
        logger.warning(
            f"GENERATED マーカー欠損のため {conflicts} 件を .conflict.md に退避",
            extra={"run_id": run_id, "stage": "export"},
        )
    logger.info(
        "export 完了",
        extra={"run_id": run_id, "stage": "export", "extra": {"items": len(rows)}},
    )


# --- ノート内容の構築 ---


def _item_note_name(digest_date: str, item_id: int, title: str | None) -> str:
    """`YYYYMMDD-{item_id}-{slug}`。slug 衝突は item_id が一意性を担保するので無害。"""
    slug = _FORBIDDEN_IN_SLUG.sub("-", title or "untitled").strip("-")[:40]
    return f"{digest_date.replace('-', '')}-{item_id}-{slug}"


def _item_frontmatter(row: sqlite3.Row, digest_date: str) -> dict:
    return {
        "id": row["item_id"],
        "title": row["title"] or "",
        "url": row["canonical_url"],  # DB の正規 URL のみ
        "source": row["source_name"],
        "source_type": row["source_type"],
        "published": row["published_at"] or "",
        "digest_date": digest_date,
        "score": row["total_score"] if row["total_score"] is not None else 0,
        "reaction": None,  # フェーズ2で 👍/🔖/✅/👎 を書き戻す予約フィールド(ユーザー所有)
    }


def _item_generated_body(row: sqlite3.Row) -> str:
    lines = ["## 要約", row["summary"] or "(なし)"]
    # X 由来は本文・抜粋を恒久保存層に置かない(Issue #2 確定)
    if row["source_type"] in ("rss", "manual") and row["extracted_text"]:
        lines += ["", "## 抜粋", row["extracted_text"][:EXCERPT_MAX_CHARS]]
    lines += ["", "## 推薦理由", row["recommendation_reason"] or "(なし)"]
    lines += ["", "## 示唆", row["implication"] or "(なし)"]
    return "\n".join(lines)


def _load_items(conn: sqlite3.Connection, item_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT items.id AS item_id, items.title, items.canonical_url, items.published_at,
               sources.name AS source_name, sources.type AS source_type,
               analyses.summary, analyses.recommendation_reason, analyses.implication,
               analyses.total_score,
               documents.extracted_text
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses ON analyses.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        LEFT JOIN documents ON documents.item_id = items.id
        WHERE items.id IN ({placeholders})
        """,
        item_ids,
    ).fetchall()
    by_id = {row["item_id"]: row for row in rows}
    return [by_id[i] for i in item_ids if i in by_id]  # 配信順を維持


# --- ファイル書き込み(GENERATED マーカー・アトミック書き) ---


def _write_note(path: Path, frontmatter: dict, generated_body: str) -> bool:
    """ノートを書く。fail-safe 退避(.conflict)した場合は False を返す。

    - 新規: frontmatter + GENERATED ブロック + 手書き領域
    - 既存: GENERATED 内のみ置換。マーカー前後の手書きは verbatim 保持、
      frontmatter はユーザー所有キーを保持して更新
    - 既存の構造が判定不能(マーカー欠損/多重・frontmatter 欠落/破損): 元ファイルに
      触れず、新版を別名の .conflict に退避する(既存 conflict も上書きしない)
    """
    generated_body = _strip_markers(generated_body)
    if not path.exists():
        _atomic_write(path, _render(frontmatter, generated_body))
        return True

    existing = path.read_text(encoding="utf-8")
    fresh = _render(frontmatter, generated_body)

    # 境界判定は fail-safe: BEGIN/END がちょうど1個ずつで正順のときだけ置換する
    if existing.count(GENERATED_BEGIN) != 1 or existing.count(GENERATED_END) != 1:
        return _write_conflict(path, fresh)
    begin = existing.find(GENERATED_BEGIN)
    end = existing.find(GENERATED_END)
    if end < begin:
        return _write_conflict(path, fresh)

    m = _FRONT_MATTER.match(existing)
    current = _parse_frontmatter(m.group(1)) if m else None
    if current is None:
        # frontmatter 欠落/破損時に {} で再構成するとユーザー追加キーが消えるため退避
        return _write_conflict(path, fresh)
    if begin < m.end():
        # マーカーが frontmatter 内にある等、境界が本文領域にない場合も誤接合せず退避
        return _write_conflict(path, fresh)

    merged = dict(current)
    for key, value in frontmatter.items():
        if key in _USER_OWNED_KEYS:
            merged.setdefault(key, value)  # ユーザー編集(reaction 等)を保持
        else:
            merged[key] = value  # 機械管理キーは更新

    pre = existing[m.end() : begin]  # BEGIN より前の手書きも verbatim 保持
    post = existing[end + len(GENERATED_END) :]
    content = (
        _render_frontmatter(merged)
        + pre
        + GENERATED_BEGIN
        + "\n"
        + generated_body
        + "\n"
        + GENERATED_END
        + post
    )
    _atomic_write(path, content)
    return True


def _write_conflict(path: Path, content: str) -> bool:
    """新版を空いている .conflict 名に退避する(既存 conflict を上書きしない)。"""
    candidate = path.with_suffix(".conflict.md")
    counter = 2
    while candidate.exists():
        candidate = path.with_suffix(f".conflict-{counter}.md")
        counter += 1
    _atomic_write(candidate, content)
    return False


def _parse_frontmatter(text: str) -> dict | None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _render_frontmatter(frontmatter: dict) -> str:
    # 文字列連結で組み立てず YAML シリアライザで生成(改行・引用符入りタイトルによる
    # frontmatter 破壊や任意キー偽造を構造的に防ぐ。設計 04 §7)
    front = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{front.rstrip()}\n---\n"


def _render(frontmatter: dict, generated_body: str) -> str:
    return (
        _render_frontmatter(frontmatter)
        + GENERATED_BEGIN
        + "\n"
        + generated_body
        + "\n"
        + GENERATED_END
        + "\n\n(この下は手書きメモ領域。再生成でも保持される)\n"
    )


def _strip_markers(content: str) -> str:
    """生成コンテンツ(LLM 出力は untrusted の影響下)からマーカー文字列を除去する。"""
    return content.replace(GENERATED_BEGIN, "").replace(GENERATED_END, "")


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
