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
- 今すぐの依頼(GH#388。設計 06 §3): 未消化一覧のボタンで受け付け(poller が呼ぶ)、
  `intel dig-now`(app.deepdive)が 1 件ずつ取り出して作る。キューの行に依頼の印と作り始めた
  印を持つ(migration 029)
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
      とき前回の観点が生きる)。今すぐの依頼(GH#388)は取り消す。作っている途中なら、
      作り始めた印は残し(作り終えるまで朝の選定がこの行を選ばない)、作り終えた
      `intel dig-now` が done にせずパスを残す(朝の生成中のパスと同じ)
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
                  now_requested_at = NULL,
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


def dismiss_from_queue(conn: sqlite3.Connection, item_id: int) -> bool:
    """深掘りカードの 👎 不要(GH#403)のキューへの反映(commit は呼び出し側)。パスにしたら True。

    - queued(調べ直し・今すぐの依頼などで掘る予定): 未消化一覧の ⏭️ 外すと同じくパスにする
      (明朝の予定に載せて掘り直さない。受付の順序の印も今の時点に進む)
    - それ以外(done・passed): 状態は変えず、受付の順序の印(backlog_op_question_id)だけを今の
      時点に進める。返事を作っている間の調べ直しの依頼(不要の前に受け付けたもの)を、作り終えた
      後にキューへ戻させないため(GH#235 と同じ照合)。不要の後に受け付けた依頼は今どおり積む。
      updated_at は進めない(done 化の compare-and-set は queued の行だけが使う)
    - キューに無い: 何もしない
    """
    row = conn.execute("SELECT status FROM dig_queue WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return False
    if row["status"] == "queued":
        apply_instructions(conn, [DigInstruction(item_id, "pass")])
        return True
    conn.execute(
        f"UPDATE dig_queue SET backlog_op_question_id = ({_BACKLOG_MARK}) WHERE item_id = ?",
        (1, item_id),
    )
    return False


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
    # 今すぐの依頼(GH#388)。now_started_at は `intel dig-now` が作り始めた印の値で、作り終えた
    # ときに、その印のままの依頼だけを閉じる照合に使う
    on_demand: bool = False
    now_started_at: str | None = None


# キューの候補の列(朝の選定と、今すぐの依頼の取り出しが共用する。GH#349・GH#388)。
# 続けて WHERE と ORDER BY を書く
_QUEUE_CANDIDATE_SELECT = """
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
"""

# その item をその JST の暦日に掘ったか(同じ日に 2 回掘らない。ノートの名前が日付と item で
# 決まり、衝突するため。Codex P2)。q.item_id と暦日の ? を使う
_DUG_ON_DAY = """
    EXISTS (
      SELECT 1 FROM dig_results r
      WHERE r.item_id = q.item_id AND date(r.created_at, '+9 hours') = ?
    )
"""


def _queue_candidate(
    row: sqlite3.Row, *, now_started_at: str | None = None
) -> DigCandidate:
    """_QUEUE_CANDIDATE_SELECT の 1 行から候補を作る。now_started_at があれば今すぐの依頼。"""
    return DigCandidate(
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
        on_demand=now_started_at is not None,
        now_started_at=now_started_at,
    )


def has_material(body: str | None, config: Config) -> bool:
    """調査段の材料が足りるか(documents の抽出本文が min_body_chars 字以上)。

    朝の選定と、深掘りカードへの調べ直しの受付(dig_answer。GH#197)、夜の未消化一覧の
    ⚠️ 本文なしの印(GH#349)が同じ条件を使う。
    """
    return bool(body) and len(body) >= config.dig.min_body_chars


def items_with_material(
    conn: sqlite3.Connection, config: Config, item_ids: Iterable[int]
) -> set[int]:
    """item のうち、調査段の材料(抽出本文)が足りるもの(has_material と同じ条件)。読むだけ。

    朝の digest の記事に今すぐのボタンを付けるか決めるのに使う(GH#388)。
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    return {
        row["item_id"]
        for row in conn.execute(
            f"SELECT item_id, extracted_text FROM documents WHERE item_id IN ({placeholders})",
            ids,
        )
        if has_material(row["extracted_text"], config)
    }


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
    `intel dig-now` が取り出した今すぐの依頼(作り始めた印のある行)は選ばない(GH#388。2 回
    作らない。途中で止まった依頼は、次の `intel dig-now` が明朝へ回してから選ばれる)。
    """
    rows = conn.execute(
        _QUEUE_CANDIDATE_SELECT
        + f"""
        WHERE q.status = 'queued' AND q.now_started_at IS NULL AND NOT {_DUG_ON_DAY}
        ORDER BY (q.requested_at IS NULL), q.requested_at DESC, q.enqueued_at DESC
        """,
        (day,),
    ).fetchall()
    selected: list[DigCandidate] = []
    skipped_requested: list[DigCandidate] = []
    for row in rows:
        if len(selected) >= limit:
            break
        candidate = _queue_candidate(row)
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


# 今すぐの依頼(GH#388。設計 06 §3)を受け付けた結果。依頼を積むのは accepted だけで、pending と
# running は callback の id だけを残す。ほかは書かない
NowRequestOutcome = Literal[
    "accepted",  # 受け付けた(poller が起動する `intel dig-now` が作る)
    "duplicate",  # 同じ callback の再配達(依頼を閉じた後でも受け付け直さない)
    "pending",  # 受け付け済み(二重押し)
    "running",  # 作っている途中
    "done",  # 掘り終えている(掘り直さない。調べ直しはカードへのリプライ。GH#137)
    "dug_today",  # 今日もう掘った(同じ日に 2 回は掘らない。_DUG_ON_DAY)
    "no_body",  # 本文が無い(材料不足。has_material)
    "no_item",  # item が無い(偽造か壊れた data)
]

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def items_with_cards(conn: sqlite3.Connection, item_ids: Iterable[int]) -> set[int]:
    """item のうち、深掘りカードを送ったことがあるもの(dig_results の message_id がある)。読むだけ。

    夜の一覧の行に ✅ 読んだを付けるか決めるのに使う(読む材料がもうある。GH#409)。
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    return {
        row["item_id"]
        for row in conn.execute(
            "SELECT DISTINCT item_id FROM dig_results "
            f"WHERE message_id IS NOT NULL AND item_id IN ({placeholders})",
            ids,
        )
    }


def dug_on(conn: sqlite3.Connection, item_id: int, day: str) -> bool:
    """その item をその JST の暦日に掘ったか(_DUG_ON_DAY と同じ条件)。"""
    return bool(
        conn.execute(
            f"SELECT {_DUG_ON_DAY} FROM (SELECT ? AS item_id) AS q", (day, item_id)
        ).fetchone()[0]
    )


def request_dig_now(
    conn: sqlite3.Connection,
    config: Config,
    item_id: int,
    day: str,
    callback_id: str,
    *,
    from_backlog: bool,
) -> NowRequestOutcome:
    """今すぐの依頼を受け付ける(GH#388。設計 06 §3)。day は押した日(JST の暦日)。

    callback_id は押した callback の id。受け付けたとき(受け付け済み・作成中と答えたときも)に行に
    残し(依頼を閉じても消さない)、同じ callback の再配達(答えた後、offset を保存する前に poller が
    止まった)を新しい依頼にしない。再配達は止まった update から順に届くので、最後の id だけを
    残せば足りる。

    状態の確認と反映は、呼び出し側の 1 つのトランザクションの中で行う(commit も呼び出し側)。
    - キューに行が無い: queued・source = 'request' で足す(本人の明示の依頼。source は初回投入の
      経路で以後不変)
    - passed: queued に戻す。本人の明示の操作なので updated_at を進める
    - queued: 依頼の印だけを付け、updated_at・観点・requested_at は変えない。朝の生成中にその
      item を掘っていても、done 化の compare-and-set を外さない(朝のカードで依頼が満たされ、
      done 化が依頼の印も閉じる)
    - done: 掘り直さない(書かない)
    requested_at は付けない。作れなかったときに `intel dig-now` が付けて、明朝の深掘りの
    ①(リプライ指定)へ回す(close_now_request)。
    from_backlog = 未消化一覧のボタンか。そうなら、状態を変える操作(足す・passed から戻す)に
    受付の順序の印を残す(apply_instructions と同じ。GH#235)。
    """
    item = conn.execute(
        "SELECT d.extracted_text FROM items i "
        "LEFT JOIN documents d ON d.item_id = i.id WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        return "no_item"
    queue = conn.execute(
        "SELECT status, now_requested_at, now_started_at, now_callback_id FROM dig_queue "
        "WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    status = queue["status"] if queue is not None else None
    if queue is not None and queue["now_callback_id"] == callback_id:
        return "duplicate"
    if status == "done":
        return "done"
    if queue is not None and (
        queue["now_started_at"] is not None or queue["now_requested_at"] is not None
    ):
        # 受け付け済み・作成中と答えた callback の id も残す。残さないと、その callback の再配達が
        # 先の依頼を閉じた後に届いたとき、新しい依頼になる
        conn.execute(
            "UPDATE dig_queue SET now_callback_id = ? WHERE item_id = ?", (callback_id, item_id)
        )
        return "running" if queue["now_started_at"] is not None else "pending"
    if dug_on(conn, item_id, day):
        return "dug_today"
    if not has_material(item["extracted_text"], config):
        return "no_body"
    if queue is None:
        conn.execute(
            "INSERT INTO dig_queue (item_id, status, source, now_requested_at, now_callback_id,"
            "  backlog_op_question_id) "
            f"VALUES (?, 'queued', 'request', {_NOW}, ?, {_BACKLOG_MARK})",
            (item_id, callback_id, int(from_backlog)),
        )
    elif status == "passed":
        conn.execute(
            f"UPDATE dig_queue SET status = 'queued', now_requested_at = {_NOW}, "
            "now_callback_id = ?, "
            f"backlog_op_question_id = COALESCE({_BACKLOG_MARK}, backlog_op_question_id), "
            f"updated_at = {_NOW} WHERE item_id = ?",
            (callback_id, int(from_backlog), item_id),
        )
    else:
        conn.execute(
            f"UPDATE dig_queue SET now_requested_at = {_NOW}, now_callback_id = ? "
            "WHERE item_id = ?",
            (callback_id, item_id),
        )
    return "accepted"


def has_now_requests(conn: sqlite3.Connection) -> bool:
    """作っていない今すぐの依頼か、作り始めた印の残った行があるか。

    poller が `intel dig-now` を起動する合図(GH#388)。印の残った行は、作っている途中か、前の
    実行が途中で止まったもの(作っている途中なら、起動した dig-now は何もせずに終わる)。
    """
    return (
        conn.execute(
            "SELECT 1 FROM dig_queue "
            "WHERE (status = 'queued' AND now_requested_at IS NOT NULL) "
            "   OR now_started_at IS NOT NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def now_requests_ahead(conn: sqlite3.Connection, item_id: int) -> int:
    """その item の今すぐの依頼より先に作る依頼の数(作っている途中の依頼を含む。GH#396)。

    今すぐの深掘りは 1 件ずつ順に作る(claim_now_request)ので、後ろの依頼は前の件の分だけ待つ。
    数える順は claim_now_request と同じ(受け付けた時刻、同じ秒は行の id)。待っている依頼の無い
    item(作り始めた・閉じた)は 0。受け付けの状態の確認と同じトランザクションの中で呼ぶ。
    """
    row = conn.execute(
        "SELECT id, now_requested_at FROM dig_queue "
        "WHERE item_id = ? AND status = 'queued' AND now_requested_at IS NOT NULL "
        "  AND now_started_at IS NULL",
        (item_id,),
    ).fetchone()
    if row is None:
        return 0
    return conn.execute(
        "SELECT count(*) FROM dig_queue WHERE item_id != ? AND ("
        "  now_started_at IS NOT NULL"
        "  OR (status = 'queued' AND now_requested_at IS NOT NULL"
        "      AND (now_requested_at < ? OR (now_requested_at = ? AND id < ?))))",
        (item_id, row["now_requested_at"], row["now_requested_at"], row["id"]),
    ).fetchone()[0]


def has_unsent_cards(conn: sqlite3.Connection) -> bool:
    """台帳に未送信のカード(未公開のノートを含む中断分)があるか。

    朝の調査段と今すぐの深掘りが再開を決めるのに使い、poller は `intel dig-now` を起動して送り
    直す合図にする(GH#388)。今すぐの依頼を朝の段が引き受けて作った分(on_demand = 0)も、
    送れなかったら朝を待たずに送り直すため、作った経路を問わない。
    """
    return (
        conn.execute("SELECT 1 FROM dig_results WHERE message_id IS NULL LIMIT 1").fetchone()
        is not None
    )


def claim_now_request(conn: sqlite3.Connection) -> DigCandidate | None:
    """最も早く受け付けた今すぐの依頼を 1 件取り出し、作り始めた印を付ける(GH#388)。

    呼び出し側のトランザクションの中で、調査段のロックを持って呼ぶ。印は 1 回押して 1 回だけ
    作るためのもので、印の残った依頼は作り直さない(interrupted_now_requests)。キューの
    スナップショット(updated_at など)は印を付ける前の値で、done 化の compare-and-set に使う
    (印は updated_at を進めない)。
    """
    row = conn.execute(
        _QUEUE_CANDIDATE_SELECT
        + """
        WHERE q.status = 'queued' AND q.now_requested_at IS NOT NULL
          AND q.now_started_at IS NULL
        ORDER BY q.now_requested_at, q.id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    started_at = conn.execute(f"SELECT {_NOW}").fetchone()[0]
    conn.execute(
        "UPDATE dig_queue SET now_started_at = ? WHERE item_id = ?",
        (started_at, row["item_id"]),
    )
    return _queue_candidate(row, now_started_at=started_at)


def interrupted_now_requests(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """作り始めた印が残った今すぐの依頼の (item id, 印)(GH#388)。

    前の `intel dig-now` が途中で止まった依頼。調査段のロックを持って呼ぶ(ロックの外では、
    作っている途中の依頼と見分けられない)。
    """
    rows = conn.execute(
        "SELECT item_id, now_started_at FROM dig_queue "
        "WHERE now_started_at IS NOT NULL ORDER BY now_started_at, id"
    ).fetchall()
    return [(row["item_id"], row["now_started_at"]) for row in rows]


def close_now_request(
    conn: sqlite3.Connection, item_id: int, now_started_at: str, *, to_morning: bool
) -> bool:
    """今すぐの依頼を閉じる(GH#388)。閉じたら True。

    作り始めた印がその値のままの依頼だけを閉じる(作っている間に本人がパスした・押し直した
    依頼は変えない)。to_morning なら、作れなかった依頼を明朝の深掘りの ①(リプライ指定)へ
    回す(requested_at を付ける。観点はそのまま)。
    """
    if to_morning:
        sql = (
            "UPDATE dig_queue SET now_requested_at = NULL, now_started_at = NULL, "
            f"requested_at = {_NOW}, updated_at = {_NOW} "
            "WHERE item_id = ? AND now_started_at = ? AND status = 'queued'"
        )
    else:
        sql = (
            "UPDATE dig_queue SET now_requested_at = NULL, now_started_at = NULL "
            "WHERE item_id = ? AND now_started_at = ?"
        )
    return conn.execute(sql, (item_id, now_started_at)).rowcount == 1


def dig_now_marked(conn: sqlite3.Connection, item_ids: Iterable[int]) -> set[int]:
    """今すぐのボタンに ✓ を付ける item(依頼を受け付けた・作っている・掘り終えた。GH#388)。"""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    return {
        row["item_id"]
        for row in conn.execute(
            "SELECT item_id FROM dig_queue "
            f"WHERE item_id IN ({placeholders}) "
            "  AND (now_requested_at IS NOT NULL OR status = 'done')",
            ids,
        )
    }
