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
- 本モジュールは delivery 層に依存しない(候補集合は呼び出し側が渡す)
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

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
            # 次の番号の前置き(「、あと」「。それと」等)を前の命令に混入させない:
            # 最後の読点・句点で切り落とす(読点のない連結は全体を観点として残す)。
            # ASCII の「.」は切らない(「GPT-5.6」等の術語を壊さない)
            cut = max(segment.rfind("、"), segment.rfind("。"), segment.rfind("\n"))
            if cut != -1:
                segment = segment[:cut]
        core = _LEAD_PARTICLE_RE.sub("", segment, count=1)
        core = _TRAIL_PUNCT_RE.sub("", core)
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


def apply_instructions(
    conn: sqlite3.Connection, instructions: list[DigInstruction]
) -> None:
    """命令列をキューへ反映する(commit は呼び出し側)。

    - request: status を queued に戻し(passed / done からの復帰を含む)、
      requested_at を現在時刻に(調査段の優先順位①)。観点は指定があれば上書き、
      なければ既存を保持(「3664 も」= 優先度だけ上げる)
    - pass: status を passed に。観点・requested_at は保持する(後で request 復帰した
      とき前回の観点が生きる)
    - source は初回投入の経路のまま変えない(migration 009 の契約。明示指定の有無は
      requested_at / status が担う)
    """
    for instruction in instructions:
        if instruction.action == "pass":
            conn.execute(
                """
                INSERT INTO dig_queue (item_id, status, source)
                VALUES (?, 'passed', 'request')
                ON CONFLICT(item_id) DO UPDATE SET
                  status = 'passed',
                  updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """,
                (instruction.item_id,),
            )
        else:
            conn.execute(
                """
                INSERT INTO dig_queue (item_id, status, source, angle, requested_at)
                VALUES (?, 'queued', 'request', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                ON CONFLICT(item_id) DO UPDATE SET
                  status = 'queued',
                  angle = COALESCE(excluded.angle, dig_queue.angle),
                  requested_at = excluded.requested_at,
                  updated_at = excluded.updated_at
                """,
                (instruction.item_id, instruction.angle),
            )


def queue_item_ids(conn: sqlite3.Connection) -> set[int]:
    """キュー在籍の item id 集合(リプライで指定可能な番号の一部)。"""
    return {row["item_id"] for row in conn.execute("SELECT item_id FROM dig_queue")}
