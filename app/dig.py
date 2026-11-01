"""深掘りキュー — 夜の入口(GH#104 / 設計 06 §2〜3)。

- 自動キュー投入: 新規 🔖 ブックマークの item 化時に無条件で積む(x_signals 段が
  呼ぶ。backfill 再読取は対象外 = 過去分は指定時のみ。設計 06 §3)
- リプライ指定: 未消化一覧への自由文リプライから「番号 + 観点 / パス」を抽出して
  キューへ反映する(poller が呼ぶ)。深掘りカードへの調べ直しの依頼も同じ規則で
  反映する(dig_answer が呼ぶ。GH#137)
- パーサは規則ベース(設計 06 §8 の実装時決定): 候補集合に実在する番号だけを命令の
  区切りとして扱い(「GPT-5」の 5 のような本文中の数字を誤検出しない)、番号の直後が
  パス語だけなら除外、それ以外は残り全文が観点。観点は自由文のまま調査段(GH#105)へ
  渡るため、LLM は使わない(poller を軽く保つ)
- 朝の選定のキューの規則(①リプライ指定 ②自動キュー。設計 06 §3)もここに置き、朝の調査段
  (app.deepdive)と夜の未消化一覧の「明朝の予定」(app.delivery.surveys)が同じ関数を呼ぶ
  (GH#349。設計 06 §2)。③ の digest の上位は朝の digest で決まるので app.deepdive に残す
- 本モジュールは delivery 層に依存しない(候補集合は呼び出し側が渡す)
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.config import Config

# パス判定: 番号の直後の残りが「(助詞)+ パス語 (+軽い述語)」だけのとき除外。
# 観点の文中に現れるパス語(「クリティカルパスの観点で」)は除外にしない
_PASS_RE = re.compile(
    r"(?:は|を|も)?\s*(パス|ぱす|pass|skip|スキップ)(で|でお願い|する|しといて|お願い)?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+")
# 番号直後の助詞剥がし: 「を」は常に(を-語頭の語は実用上ない)、他の助詞は直後に
# 区切り(空白・読点・文末)があるときだけ 1 つ剥がす。「はてな」「もっと」のような
# 語頭の一致を食わないための限定
_LEAD_PARTICLE_RE = re.compile(
    r"^[\s、,。.::]*(?:を|(?:は|が|も|の|に|で|と)(?=[\s、,。.]|$))?\s*"
)
_TRAIL_PUNCT_RE = re.compile(r"[\s、,。.!!??]*$")
# 次の番号の前置きになる接続語(GH#238)。最後以外の命令で、読点の後ろがこれだけの
# ときに限り前置きとして切り落とす(読点の後ろに自由文が続くなら観点の一部として残す)
_CONNECTOR_WORDS = (
    "あとは", "あと", "それと", "それから", "それに", "そして", "で", "次に", "次は",
    "ついでに", "さらに", "加えて", "また", "やっぱり", "えっと", "えーと", "あ",
)
_CONNECTOR_ONLY_RE = re.compile(
    r"\s*(?:(?:" + "|".join(_CONNECTOR_WORDS) + r")\s*)*"
)


@dataclass(frozen=True)
class DigInstruction:
    """リプライから抽出した 1 命令。action は 'request'(登録)| 'pass'(除外)。"""

    item_id: int
    action: str
    angle: str | None = None


def parse_dig_reply(text: str, valid_ids: set[int]) -> list[DigInstruction]:
    """自由文リプライを命令列に変換する(候補にない番号は本文扱いで無視)。

    番号の出現位置で本文を区切り、各番号の直後〜次の番号までをその番号への指示と
    みなす(話し言葉の語順「3664 を◯◯の観点で」前提)。同じ番号が複数回現れたら
    後勝ち(「3664 はパス。やっぱり 3664 を◯◯で」= 登録)。
    """
    boundaries = [
        m for m in _NUMBER_RE.finditer(text) if int(m.group()) in valid_ids
    ]
    ordered: dict[int, DigInstruction] = {}
    for position, match in enumerate(boundaries):
        item_id = int(match.group())
        is_last = position + 1 >= len(boundaries)
        end = len(text) if is_last else boundaries[position + 1].start()
        segment = text[match.end() : end]
        if not is_last:
            # 次の番号の前置き(「。それと」「、あと」等)を前の命令に混入させない。
            # 切るのは命令どうしの区切りと分かるところだけ(GH#238):
            # - 句点・改行は文の区切りなので、最後のものより後ろを切る。ASCII の「.」は
            #   切らない(「GPT-5.6」等の術語を壊さない)
            # - 読点は観点の中にも現れる(「コスト、運用の観点で」)ので、後ろが接続語
            #   だけのときに限り切る。読点で分けて末尾の区間から 1 回ずつ判定する
            #   (末尾に固定した正規表現の探索は長い入力で二乗になる)
            cut = max(segment.rfind("。"), segment.rfind("\n"))
            if cut != -1:
                segment = segment[:cut]
            parts = segment.split("、")
            while len(parts) > 1 and _CONNECTOR_ONLY_RE.fullmatch(parts[-1]):
                parts.pop()
            segment = "、".join(parts)
        core = _LEAD_PARTICLE_RE.sub("", segment, count=1)
        core = _TRAIL_PUNCT_RE.sub("", core)
        if not is_last:
            # パスは観点をとらないので、パス語の直後の読点はそこで命令を終える
            # (「3773 はパス、気になるのは 3660 の方」の後半は次の番号の前置き)
            head = _TRAIL_PUNCT_RE.sub("", core.partition("、")[0])
            if _PASS_RE.fullmatch(head):
                core = head
        if _PASS_RE.fullmatch(core):
            instruction = DigInstruction(item_id, "pass")
        else:
            instruction = DigInstruction(item_id, "request", angle=core or None)
        ordered.pop(item_id, None)  # 再指定は後勝ち。順序も最後の言及位置に更新
        ordered[item_id] = instruction
    return list(ordered.values())


def enqueue_bookmark(conn: sqlite3.Connection, item_id: int) -> bool:
    """新規 🔖 の自動キュー投入(冪等)。既存行(passed / done 含む)は変更しない。

    パス済み item への再ブックマークで status を戻さない: パスはユーザーの明示指示で
    あり、自動投入がそれを覆すと「パスしたのに翌朝掘られる」が起きる。復帰はリプライ
    指定(request)のみ。
    """
    inserted = conn.execute(
        "INSERT OR IGNORE INTO dig_queue (item_id, source) VALUES (?, 'bookmark')",
        (item_id,),
    ).rowcount
    return inserted == 1


# 受付の順序の印(GH#235)。未消化一覧への操作なら受け付け済みの質問の id の最大(質問が
# まだ無ければ 0)、そうでなければ NULL にして、既存の行では前の印を残す
_BACKLOG_MARK = "CASE WHEN ? THEN (SELECT coalesce(max(id), 0) FROM dig_questions) END"
_KEEP_MARK = "COALESCE(excluded.backlog_op_question_id, dig_queue.backlog_op_question_id)"


def apply_instructions(
    conn: sqlite3.Connection,
    instructions: list[DigInstruction],
    *,
    from_backlog: bool = True,
) -> None:
    """命令列をキューへ反映する(commit は呼び出し側)。

    - request: status を queued に戻し(passed / done からの復帰を含む)、
      requested_at を現在時刻に(調査段の優先順位①)。観点は指定があれば上書き、
      なければ既存を保持(「3664 も」= 優先度だけ上げる)
    - pass: status を passed に。観点・requested_at は保持する(後で request 復帰した
      とき前回の観点が生きる)
    - source は初回投入の経路のまま変えない(migration 009 の契約。明示指定の有無は
      requested_at / status が担う)
    - from_backlog = 未消化一覧への本人の操作か(既定)。そうなら受付の順序の印として、
      反映した時点で受け付け済みの質問(dig_questions.id)の最大を残す(migration 022)。
      深掘りカードへの調べ直しの依頼(dig_answer)は False で、印を変えない。返事を作る間の
      未消化一覧への操作を、作り終えた古い依頼で上書きしないための照合に使う(GH#235)
    """
    for instruction in instructions:
        if instruction.action == "pass":
            conn.execute(
                f"""
                INSERT INTO dig_queue (item_id, status, source, backlog_op_question_id)
                VALUES (?, 'passed', 'request', {_BACKLOG_MARK})
                ON CONFLICT(item_id) DO UPDATE SET
                  status = 'passed',
                  backlog_op_question_id = {_KEEP_MARK},
                  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """,
                (instruction.item_id, int(from_backlog)),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO dig_queue
                  (item_id, status, source, angle, requested_at, backlog_op_question_id)
                VALUES (?, 'queued', 'request', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                        {_BACKLOG_MARK})
                ON CONFLICT(item_id) DO UPDATE SET
                  status = 'queued',
                  angle = COALESCE(excluded.angle, dig_queue.angle),
                  requested_at = excluded.requested_at,
                  backlog_op_question_id = {_KEEP_MARK},
                  updated_at = excluded.updated_at
                """,
                (instruction.item_id, instruction.angle, int(from_backlog)),
            )


def queue_item_ids(conn: sqlite3.Connection) -> set[int]:
    """キュー在籍の item id 集合(リプライで指定可能な番号の一部)。"""
    return {row["item_id"] for row in conn.execute("SELECT item_id FROM dig_queue")}


@dataclass(frozen=True)
class DigCandidate:
    item_id: int
    angle: str | None
    requested: bool  # リプライ指定(requested_at あり)
    # done 化の compare-and-set 用スナップショット(updated_at は秒解像度のため、
    # 同一秒内の更新も検出できるよう status 以外の実値も比較する。Codex P2)。
    # digest の上位から選んだ候補(from_digest)はキューの行が無いので None
    queue_updated_at: str | None
    queue_requested_at: str | None
    title: str | None
    author: str | None
    published_at: str | None
    canonical_url: str
    body: str | None  # documents.extracted_text
    language: str | None
    title_ja: str | None  # 既存分析(あれば)
    summary: str | None
    # 同じ item の前回の解説ノート(調べ直し = done からの再指定。GH#137)。新しいノートからリンクする
    previous_note_name: str | None = None
    # 本文を取得・抽出した JST の日付(documents.extracted_at)。原文・対訳ノートの fetched に
    # 書く(深掘りした日ではない。GH#239)
    fetched_date: str | None = None
    # その日の digest の上位から選んだ(設計 06 §3 の ③。GH#349)。キューの行は記帳のときに足す
    from_digest: bool = False


def has_material(body: str | None, config: Config) -> bool:
    """調査段の材料が足りるか(documents の抽出本文が min_body_chars 字以上)。

    朝の選定と、深掘りカードへの調べ直しの受付(dig_answer。GH#197)、夜の未消化一覧の
    ⚠️ 本文なしの印(GH#349)が同じ条件を使う。
    """
    return bool(body) and len(body) >= config.dig.min_body_chars


def select_from_queue(
    conn: sqlite3.Connection, config: Config, limit: int, day: str
) -> tuple[list[DigCandidate], list[DigCandidate]]:
    """キュー(設計 06 §3 の ①②)から、優先順に材料十分な candidate を limit 件選ぶ。読むだけ。

    戻り値 = (選定, 材料不足のリプライ指定)。材料不足の自動選出は枠を消費せず
    静かに残す(未消化一覧に見えている。ここで毎朝 ERROR を出すとノイズになる)。
    同日に実行済みの item は選ばない(done 直後の再指定を同日中に再実行すると
    ノート名が衝突し、カードが古いノートを指しうる。Codex P2。翌朝に掘られる =
    「夜選ぶ → 朝届く」のリズム通り)。
    朝の調査段と夜の未消化一覧の「明朝の予定」が共用する(GH#349。next_morning_plan)。
    """
    rows = conn.execute(
        """
        SELECT q.item_id, q.angle, q.requested_at, q.updated_at,
               i.title, i.author, i.published_at, i.canonical_url,
               d.extracted_text, d.language,
               date(d.extracted_at, '+9 hours') AS fetched_date,
               a.title_ja, a.summary,
               (SELECT r.note_name FROM dig_results r
                WHERE r.item_id = q.item_id AND r.published_at IS NOT NULL
                ORDER BY r.id DESC LIMIT 1) AS previous_note_name
        FROM dig_queue q
        JOIN items i ON i.id = q.item_id
        LEFT JOIN documents d ON d.item_id = q.item_id
        LEFT JOIN analyses a ON a.id = (
            SELECT max(id) FROM analyses
            WHERE item_id = q.item_id AND degraded = 0
        )
        WHERE q.status = 'queued'
          AND NOT EXISTS (
            SELECT 1 FROM dig_results r
            WHERE r.item_id = q.item_id AND date(r.created_at, '+9 hours') = ?
          )
        ORDER BY (q.requested_at IS NULL), q.requested_at DESC, q.enqueued_at DESC
        """,
        (day,),
    ).fetchall()
    selected: list[DigCandidate] = []
    skipped_requested: list[DigCandidate] = []
    for row in rows:
        if len(selected) >= limit:
            break
        candidate = DigCandidate(
            item_id=row["item_id"],
            angle=row["angle"],
            requested=row["requested_at"] is not None,
            queue_updated_at=row["updated_at"],
            queue_requested_at=row["requested_at"],
            title=row["title"],
            author=row["author"],
            published_at=row["published_at"],
            canonical_url=row["canonical_url"],
            body=row["extracted_text"],
            language=row["language"],
            title_ja=row["title_ja"],
            summary=row["summary"],
            previous_note_name=row["previous_note_name"],
            fetched_date=row["fetched_date"],
        )
        if has_material(candidate.body, config):
            selected.append(candidate)
        elif candidate.requested:
            skipped_requested.append(candidate)
        # 材料不足の自動選出は対象外のまま(キューに残る。docstring 参照)
    return selected, skipped_requested


def next_morning_plan(conn: sqlite3.Connection, config: Config, day: str) -> list[DigCandidate]:
    """明朝の予定(GH#349。設計 06 §2): キューから朝の選定と同じ規則で選ぶ、翌日の上限の件数ぶん。

    day は翌朝の JST の暦日。夜の時点では翌日の実行数は 0 なので、件数は `[dig].daily_cap`。
    ③(digest の上位)は朝の digest で決まるので含めない。読むだけでキューは変えない。
    """
    if config.dig.daily_cap <= 0:
        return []
    planned, _ = select_from_queue(conn, config, config.dig.daily_cap, day)
    return planned


# 未消化一覧の各行の深掘りの状態(GH#349。設計 06 §2)。None = 印なし(キューに無く本文もある)
DigState = Literal["planned", "waiting", "done", "passed", "no_body"]


def dig_states(
    conn: sqlite3.Connection,
    config: Config,
    item_ids: Iterable[int],
    planned_ids: set[int],
) -> dict[int, DigState | None]:
    """item ごとの深掘りの状態(GH#349。設計 06 §2)。読むだけ。

    - planned: 明朝の予定(planned_ids。next_morning_plan の結果)
    - done / passed: dig_queue の status のまま(本文の有無を問わない)
    - waiting: queued で本文もあるが、予定の枠の外
    - no_body: queued のまま、またはキューに無い item で、材料不足(has_material と同じ条件)
    - None: キューに無く本文もある(リプライで指定すれば掘る)
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT i.id AS item_id, q.status, d.extracted_text
        FROM items i
        LEFT JOIN dig_queue q ON q.item_id = i.id
        LEFT JOIN documents d ON d.item_id = i.id
        WHERE i.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    states: dict[int, DigState | None] = {}
    for row in rows:
        item_id = row["item_id"]
        status = row["status"]
        if item_id in planned_ids:
            states[item_id] = "planned"
        elif status in ("done", "passed"):
            states[item_id] = status
        elif not has_material(row["extracted_text"], config):
            states[item_id] = "no_body"
        elif status == "queued":
            states[item_id] = "waiting"
        else:
            states[item_id] = None
    return states
