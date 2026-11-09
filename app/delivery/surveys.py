"""夜アンケート・🔖追い掛け・watchdog・朝バッチ失敗通知・週 1 回の読んだのまとめ確認
(設計 04 §4 / 05 §1.2, §4)。

- 起動は受信デーモン(intel poll)の内蔵タイマー(送信経路をポーラーに集約)。
  時刻は config([telegram].survey_time / watchdog_time。JST)
- 送信済み管理は telegram_state の日付キー。**送信前に記録する**(部分送信の再送で
  同じ質問を連打するより、失敗日は未送信 = 未回答の fail-closed 集計(PROPOSAL)に
  倒す。Telegram 送信自体のリトライは TelegramApi が3回行う)
- 通知のレート制限: 同一種別1日1通(設計 05 §4)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import NamedTuple

from app import source_health
from app.config import Config
from app.delivery.state import get_state, set_state
from app.delivery.telegram import (
    TITLE_MAX_CHARS,
    DeliveryError,
    TelegramApi,
    escape_and_truncate,
    escape_attr,
    pack_line_groups,
    visible_length,
)
from app.dig import DigCandidate, DigState, dig_states, dug_on, next_morning_plan
from app.jst import JST, jst_day_bounds_utc, today_jst
from app.source_health import X_FAILURE_ALERT, source_failure_alerts

STATE_SURVEY_SENT = "survey_sent_date"
STATE_WATCHDOG_NOTIFIED = "watchdog_notified_date"
STATE_RUN_FAILURE_NOTIFIED = "run_failure_notified_date"
STATE_SOURCE_REPORT_SENT = "source_report_sent_date"
STATE_READ_CONFIRM_SENT = "read_confirm_sent_date"

READ_MIN_CHOICES = ("5", "10", "15", "20", "30")  # 質問2の5択(設計 04 §4)

# 未消化 🔖 一覧(GH#88)。可視テキストを 4096 字制限に収める(URL は href 側で不算入)
DIG_BACKLOG_MAX_ITEMS = 15
DIG_BACKLOG_TITLE_MAX_CHARS = 80
# 1 通の可視の長さの上限(Telegram の 4096 字に余裕を残す)。明朝の予定の行は上限 15 件に
# 数えないので、超えたら行の単位で次の 1 通に分ける(GH#349)
DIG_BACKLOG_TEXT_MAX_CHARS = 4000
DIG_BACKLOG_CONTINUED = "🔖 未消化の深掘り候補(続き)"
# 一覧の行のボタン(明朝の予定の行の skip = GH#349、今すぐ = GH#388。設計 06 §2・04 §3)。1 通の
# ボタンの数の上限は、Telegram の inline keyboard の上限(約 100)に余裕を残す。超えるなら行の
# 単位で次の 1 通に分ける
DIG_BACKLOG_MAX_BUTTONS = 50
# 今すぐのボタンを付ける行の状態(本文があり、掘り終えておらず、パスもしていない。GH#388)。
# 明朝の予定の行(planned)は skip のボタンと並べる
DIG_NOW_STATES: frozenset[DigState | None] = frozenset({"planned", "waiting", None})
# 一覧から消える操作の案内(GH#398)。深掘りの段が動いているときだけ、注記の最後に置く。カードへの
# リプライは、返事をしない設定・返事の上限を超えた日は感想として残るだけで take にならず、一覧から
# 外れない。どの設定でも確かに外れるボタンだけを案内する
DIG_BACKLOG_READ_NOTE = "✅ 読み終えたら、深掘りカードの「✅ 読んだ」を押すと一覧から外れます"
# 未消化一覧の各行の深掘りの状態の印(GH#349。設計 06 §2)。None(キューに無く本文もある)は印なし
DIG_STATE_MARKS: dict[DigState, str] = {
    "planned": "🗓 明朝",
    "waiting": "⏳ 待ち",
    "done": "✅ 掘った",
    "passed": "⏭️ パス",
    "no_body": "⚠️ 本文なし",
}

# watchdog の 1 通の上限。Telegram の 4096 字に余裕を残す(超えると試し直すたびに送信が
# 失敗し、watchdog が届かない。GH#124 でソースの行が可変長になった)
WATCHDOG_TEXT_MAX_CHARS = 4000
SOURCE_REPORT_MAX_CHARS = 4000

# 週 1 回の読んだのまとめ確認(GH#399。設計 04 §4・06 §2)。日曜の夜アンケート(未消化一覧)の後に
# 1 通。載せる行は未消化一覧と同じく 15 件まで(行のボタン 15 個と全部のボタン 1 個)
READ_CONFIRM_WEEKDAY = 6  # datetime.weekday() の日曜
READ_CONFIRM_MAX_ITEMS = 15
READ_CONFIRM_BUTTONS_PER_ROW = 3
READ_CONFIRM_ALL_LABEL = "✅ 全部読んだにする"


def _mark(text: str, is_selected: bool) -> str:
    return f"{text} ✓" if is_selected else text


def q1_keyboard(date_compact: str, *, selected: str | None = None) -> dict:
    """質問1(巡回した?)。selected は y|n(✓ 反映用。設計 04 §3)。"""
    return {
        "inline_keyboard": [[
            {"text": _mark("はい", selected == "y"), "callback_data": f"q1:{date_compact}:y"},
            {"text": _mark("いいえ", selected == "n"), "callback_data": f"q1:{date_compact}:n"},
        ]]
    }


def q2_keyboard(date_compact: str, *, selected: str | None = None) -> dict:
    """質問2(読了時間)。selected は分数文字列。"""
    return {
        "inline_keyboard": [[
            {
                "text": _mark(f"{minutes}分", selected == minutes),
                "callback_data": f"q2:{date_compact}:{minutes}",
            }
            for minutes in READ_MIN_CHOICES
        ]]
    }


def followup_keyboard(item_id: int, *, selected: str | None = None) -> dict:
    """🔖追い掛け(深掘りした?)。selected は y|n。"""
    return {
        "inline_keyboard": [[
            {"text": _mark("はい", selected == "y"), "callback_data": f"f:{item_id}:y"},
            {"text": _mark("いいえ", selected == "n"), "callback_data": f"f:{item_id}:n"},
        ]]
    }


class BacklogButtons(NamedTuple):
    """未消化一覧の 1 行のボタン。

    now = ⚡ 今すぐ(GH#388)、skip = ⏭️ 外す(明朝の予定の行。GH#349)。
    """

    item_id: int
    now: bool
    skip: bool

    @property
    def count(self) -> int:
        return int(self.now) + int(self.skip)


def backlog_keyboard(
    rows: Sequence[BacklogButtons],
    *,
    passed: Collection[int] = (),
    now_marked: Collection[int] = (),
) -> dict:
    """未消化一覧の 1 通のボタン。その 1 通に載せた行ごとに 1 列(⚡ 今すぐ・⏭️ 外す の順)。

    押した後の盤面(設計 04 §3)では、外した item(passed)の skip と、今すぐの依頼を受け付けた・
    掘り終えた item(now_marked)の今すぐに ✓ を付ける。
    """
    keyboard: list[list[dict]] = []
    for row in rows:
        buttons = []
        if row.now:
            buttons.append({
                "text": _mark(f"⚡ {row.item_id} を今すぐ", row.item_id in now_marked),
                "callback_data": f"b:{row.item_id}:n",
            })
        if row.skip:
            buttons.append({
                "text": _mark(f"⏭️ {row.item_id} を外す", row.item_id in passed),
                "callback_data": f"b:{row.item_id}:p",
            })
        keyboard.append(buttons)
    return {"inline_keyboard": keyboard}


def run_due_timers(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    now: datetime,
    *,
    unrecorded_read_confirms: list[SentReadConfirm] | None = None,
) -> None:
    """期限が来たタイマー(夜アンケート・watchdog・週 1 回のソースの健康診断と読んだのまとめ
    確認)を実行する。
    poll ループから毎周期呼ぶ(失敗した後は受信と別に間を空けて試し直す。poller の
    TIMER_RETRY_S。GH#224)。

    now は JST aware。ポーラー停止中に時刻を跨いだ場合は次の起動周期で送る(遅延送信)。
    日付を跨いだ分は送らない(昨日の夜アンケートを今日送っても回答が汚れるだけ)。
    unrecorded_read_confirms は、送れたのに台帳へ記録できなかったまとめ確認(GH#399)。run_poller が
    周期をまたいで持ち、ここで記録だけを試し直す(送り直さない)。
    """
    today = now.strftime("%Y-%m-%d")
    if unrecorded_read_confirms is None:
        unrecorded_read_confirms = []
    if _is_due(now, config.telegram.survey_time) and get_state(conn, STATE_SURVEY_SENT) != today:
        set_state(conn, STATE_SURVEY_SENT, today)
        send_evening_survey(conn, config, api, logger, now)
    if (
        _is_due(now, config.telegram.watchdog_time)
        and get_state(conn, STATE_WATCHDOG_NOTIFIED) != today
    ):
        problems = _watchdog_problems(conn, config, today)
        if problems:
            # 通知系は送信成功後に state を立てる(失敗した日に沈黙しない。夜アンケートの
            # 送信前記録とは目的が逆: 通知は届くまで再試行してよい)。
            # 同一種別1日1通なので、見つけた問題は1通にまとめる
            api.send_message(
                _require_chat_id(config),
                escape_and_truncate(
                    "\n".join(f"⚠️ watchdog: {p}" for p in problems), WATCHDOG_TEXT_MAX_CHARS
                ),
            )
            set_state(conn, STATE_WATCHDOG_NOTIFIED, today)
            logger.warning(f"watchdog 通知: {' / '.join(problems)}", extra={"stage": "poll"})
    # 週 1 回のソースの健康診断(GH#150)。夜アンケートと同じ時刻。日曜を逃したらその週は送らない
    # (夜アンケートと同じく、日付を跨いだ分は送らない)。通知系なので送れた後に state を立てる
    if (
        now.weekday() == source_health.REPORT_WEEKDAY
        and _is_due(now, config.telegram.survey_time)
        and get_state(conn, STATE_SOURCE_REPORT_SENT) != today
    ):
        report = source_health.source_health_report(conn, config, today)
        api.send_message(
            _require_chat_id(config), escape_and_truncate(report, SOURCE_REPORT_MAX_CHARS)
        )
        set_state(conn, STATE_SOURCE_REPORT_SENT, today)
        logger.info("ソースの健康診断を送った", extra={"stage": "poll"})
    # 週 1 回の読んだのまとめ確認(GH#399)。日曜の夜アンケート(未消化一覧)の後。日曜を逃したら
    # その週は送らない。送れた後に state を立てる(健康診断と同じ。対象が無い週も立てる)。
    # 送れたのに記録できなかった分は、曜日を問わず先に記録する(日付を跨いでも 1 通のボタンを生かす)
    record_unrecorded_read_confirms(conn, unrecorded_read_confirms)
    if (
        now.weekday() == READ_CONFIRM_WEEKDAY
        and _is_due(now, config.telegram.survey_time)
        and get_state(conn, STATE_READ_CONFIRM_SENT) != today
    ):
        send_read_confirm(conn, config, api, logger, today, unrecorded_read_confirms)
        set_state(conn, STATE_READ_CONFIRM_SENT, today)


def send_evening_survey(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    now: datetime,
) -> None:
    """質問1・質問2、続けて 🔖追い掛けを送る(設計 04 §4)。"""
    chat_id = _require_chat_id(config)
    date_compact = now.strftime("%Y%m%d")
    api.send_message(
        chat_id,
        "📝 今日、自分から情報を探しに巡回した?\n(ダイジェストを読むのは含めない)",
        reply_markup=q1_keyboard(date_compact),
    )
    api.send_message(
        chat_id,
        "📝 今朝のダイジェストを読むのにかかった時間は?\n(ざっくりでOK)",
        reply_markup=q2_keyboard(date_compact),
    )
    backlog = pending_dig_items(conn)
    # 明朝の予定(GH#349)。夜の時点のキューを朝の選定と同じ規則で並べる(読むだけ)
    plan = next_morning_plan(conn, config, (now + timedelta(days=1)).strftime("%Y-%m-%d"))
    backlog_failure: DeliveryError | None = None
    # 0件で予定も無い日は送らない(GH#88・GH#349)。長ければ行の単位で複数通に分ける(GH#349)。
    # 行のボタン(明朝の予定の skip = GH#349、今すぐ = GH#388)は、その行を載せた 1 通に付ける
    today = now.strftime("%Y-%m-%d")
    for text, keyboard in _dig_backlog_message(conn, config, backlog, plan, today):
        try:
            message_id = api.send_message(chat_id, text, reply_markup=keyboard)
        except DeliveryError as e:
            # 一覧の失敗で追い掛けを止めない。送れた分は台帳に残っている。失敗は追い掛けの
            # 後に投げ直し、内蔵タイマーの失敗として残す(夜アンケートは送信前に記録済みなので
            # 一覧は送り直さない。モジュールの docstring)
            backlog_failure = e
            break
        # リプライ先の認識用台帳(GH#104)。過去の一覧へのリプライも有効にするため
        # 蓄積する(REPLACE は getUpdates 再配達等での同一 id 再送に対する冪等)。複数通に
        # 分けたときは、どの 1 通へのリプライも指定として受け付ける
        conn.execute(
            "INSERT OR REPLACE INTO dig_backlog_messages (message_id, sent_date) "
            "VALUES (?, ?)",
            (message_id, today),
        )
        conn.commit()
    followups = _followup_targets(conn, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    for row in followups:
        api.send_message(
            chat_id,
            f"🔖 深掘りした?\n{_linked_title(row, TITLE_MAX_CHARS)}",
            reply_markup=followup_keyboard(row["item_id"]),
        )
    logger.info(
        "夜アンケート送信",
        extra={
            "stage": "poll",
            "extra": {
                "followups": len(followups),
                "dig_backlog": len(backlog),
                "dig_plan": len(plan),
            },
        },
    )
    if backlog_failure is not None:
        raise backlog_failure


def read_confirm_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """読んだのまとめ確認の対象(GH#399): 未消化(pending_dig_items)のうち、Telegram の感想が
    ある item。並びは未消化一覧と同じ。

    感想は item_notes のリプライで、深掘りカードへの質問・依頼として台帳(dig_questions)に
    積んだリプライは除く(質問は読んだ印にしない。ユーザーの判断)。返事をしない設定・上限を超えた
    日のカードへのリプライは、質問として積まずに感想として残るので含む。
    """
    rows = pending_dig_items(conn)
    if not rows:
        return []
    placeholders = ", ".join("?" for _ in rows)
    noted = {
        row["item_id"]
        for row in conn.execute(
            "SELECT DISTINCT n.item_id FROM item_notes n "
            f"WHERE n.item_id IN ({placeholders}) AND NOT EXISTS ("
            "  SELECT 1 FROM dig_questions q WHERE q.question_message_id = n.message_id)",
            [row["item_id"] for row in rows],
        )
    }
    return [row for row in rows if row["item_id"] in noted]


def read_confirm_keyboard(item_ids: Sequence[int], read: Collection[int]) -> dict:
    """読んだのまとめ確認の 1 通のボタン(GH#399)。行ごとの ✅ {id} と、最後の行に全部。

    読んだにしてある item(read)の行は ✓ を付け、押すと取り消す(c:{id}:u)。押していない行は
    読んだにする(c:{id}:r)。全部(c:0:a)は、載せた item のうち読んでいないものを読んだにする。
    """
    buttons = [
        {"text": _mark(f"✅ {item_id}", item_id in read),
         "callback_data": f"c:{item_id}:{'u' if item_id in read else 'r'}"}
        for item_id in item_ids
    ]
    rows = [
        buttons[i:i + READ_CONFIRM_BUTTONS_PER_ROW]
        for i in range(0, len(buttons), READ_CONFIRM_BUTTONS_PER_ROW)
    ]
    all_read = bool(item_ids) and all(item_id in read for item_id in item_ids)
    rows.append([{"text": _mark(READ_CONFIRM_ALL_LABEL, all_read), "callback_data": "c:0:a"}])
    return {"inline_keyboard": rows}


def _read_confirm_message(rows: Sequence[Mapping]) -> tuple[str, list[int]]:
    """読んだのまとめ確認の本文と、載せた item の id(載せた順)。"""
    shown = list(rows[:READ_CONFIRM_MAX_ITEMS])
    lines = [
        f"📝 感想を書いた未消化({len(rows)}件)",
        "感想をリプライした記事です。読み終えたものは ✅ を押すと、未消化一覧から外れます",
        "",
        *(f"{row['item_id']}. {_linked_title(row, DIG_BACKLOG_TITLE_MAX_CHARS)}" for row in shown),
    ]
    if len(rows) > len(shown):
        lines.append(f"(他 {len(rows) - len(shown)} 件は来週の確認に回します)")
    return "\n".join(lines), [row["item_id"] for row in shown]


class SentReadConfirm(NamedTuple):
    """送れたまとめ確認の 1 通(台帳 read_confirm_messages の 1 行。GH#399)。"""

    message_id: int
    sent_date: str
    item_ids: list[int]


def send_read_confirm(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    day: str,
    unrecorded: list[SentReadConfirm],
) -> None:
    """読んだのまとめ確認を 1 通送り、載せた item を台帳に記録する(GH#399)。対象が無ければ送らない。

    台帳(read_confirm_messages)は、ボタンを受け付けるときの照合と盤面の作り直しに使う。
    送り直さない: その日の台帳の行があれば送らない(送って記録した後、送信済みの state だけを
    立てられなかったとき)。送れたのに記録できなかったとき(DB のロック競合)は、unrecorded に
    残して投げ直し、次の周期の record_unrecorded_read_confirms が記録だけを試し直す。
    """
    if conn.execute(
        "SELECT 1 FROM read_confirm_messages WHERE sent_date = ? LIMIT 1", (day,)
    ).fetchone() is not None or any(sent.sent_date == day for sent in unrecorded):
        logger.info("読んだのまとめ確認は送り済み(送り直さない)", extra={"stage": "poll"})
        return
    rows = read_confirm_items(conn)
    if not rows:
        logger.info("読んだのまとめ確認の対象なし(送らない)", extra={"stage": "poll"})
        return
    text, item_ids = _read_confirm_message(rows)
    message_id = api.send_message(
        _require_chat_id(config), text, reply_markup=read_confirm_keyboard(item_ids, ())
    )
    unrecorded.append(SentReadConfirm(message_id, day, item_ids))
    logger.info(
        "読んだのまとめ確認を送った",
        extra={"stage": "poll", "extra": {"items": len(item_ids), "candidates": len(rows)}},
    )
    record_unrecorded_read_confirms(conn, unrecorded)


def record_unrecorded_read_confirms(
    conn: sqlite3.Connection, unrecorded: list[SentReadConfirm]
) -> None:
    """送れたのに台帳へ記録していないまとめ確認を記録する(GH#399)。記録できた分は unrecorded から
    除く。

    DB の失敗は投げる(内蔵タイマーの失敗として、次の周期に試し直す)。poller が止まると
    unrecorded は失われ、その 1 通のボタンは受け付けなくなる(稀。無配信より二重送信の方針の
    範囲で、次の起動がその日の分を送り直す)。
    """
    while unrecorded:
        sent = unrecorded[0]
        conn.execute(
            "INSERT OR REPLACE INTO read_confirm_messages (message_id, sent_date, item_ids) "
            "VALUES (?, ?, ?)",
            (sent.message_id, sent.sent_date, json.dumps(sent.item_ids)),
        )
        conn.commit()
        unrecorded.pop(0)


def _linked_title(row: Mapping, budget: int) -> str:
    """タイトルを深掘り動線(canonical_url へのリンク)にする(GH#88)。"""
    title = escape_and_truncate(
        (row["title_ja"] or row["title"] or "(タイトルなし)").strip(), budget
    )
    return f'<a href="{escape_attr(row["canonical_url"])}">{title}</a>'


def _dig_backlog_message(
    conn: sqlite3.Connection,
    config: Config,
    backlog: list[sqlite3.Row],
    plan: Sequence[DigCandidate],
    today: str,
) -> list[tuple[str, dict | None]]:
    """未消化一覧の本文とボタン(1 通以上)。空 = 送らない(🔖 の一覧が 0 件で、明朝の予定も無い日)。

    today は一覧を送る日(JST)。今日もう掘った item には今すぐのボタンを付けない(同じ日に 2 回は
    掘らない。押しても受け付けないため。GH#388)。

    `[dig].daily_cap = 0`(深掘りの段が止まっている)なら印も予定も出さず、今までの一覧のまま
    にする(GH#349。設計 06 §2)。予定の行は先頭に置いて上限(DIG_BACKLOG_MAX_ITEMS)に数えず、
    🔖 の一覧に無い予定(カードからの調べ直しの依頼など)も行として足す。予定の行の番号も
    キューにあるので、一覧へのリプライで指定できる(poller の valid_ids は queue_item_ids を含む)。
    """
    if config.dig.daily_cap <= 0:
        return _dig_backlog_text(backlog) if backlog else []
    if not backlog and not plan:
        return []
    by_id = {row["item_id"]: row for row in backlog}
    planned_ids = {candidate.item_id for candidate in plan}
    plan_rows: list[Mapping] = [
        by_id.get(candidate.item_id)
        or {
            "item_id": candidate.item_id,
            "title": candidate.title,
            "title_ja": candidate.title_ja,
            "canonical_url": candidate.canonical_url,
        }
        for candidate in plan
    ]
    rest = [row for row in backlog if row["item_id"] not in planned_ids]
    shown_ids = [row["item_id"] for row in rest[:DIG_BACKLOG_MAX_ITEMS]]
    return _dig_backlog_text(
        rest,
        plan_rows=plan_rows,
        states=dig_states(conn, config, shown_ids, planned_ids),
        digest_fill=config.dig.daily_cap - len(plan),
        with_buttons=True,
        dug_today={
            item_id for item_id in [*planned_ids, *shown_ids] if dug_on(conn, item_id, today)
        },
    )


def _dig_backlog_text(
    rows: Sequence[Mapping],
    *,
    plan_rows: Sequence[Mapping] = (),
    states: Mapping[int, DigState | None] | None = None,
    digest_fill: int = 0,
    with_buttons: bool = False,
    dug_today: Collection[int] = (),
) -> list[tuple[str, dict | None]]:
    """未消化 🔖 一覧。新しい 🔖 順・上限超過分は件数だけ示す(GH#88)。

    番号は item id(GH#104)。表示位置の連番だと日ごとに変わり、リプライ指定
    「3664 を◯◯の観点で」が翌日以降の一覧や記憶と食い違うため、安定な id を使う。
    GH#349: states があれば番号の後に深掘りの状態の印を添える。plan_rows(明朝の予定)は
    先頭に 🗓 明朝 で置き、上限に数えない。rows は予定を除いた残りの行。digest_fill は
    予定が翌日の上限に足りない件数(明朝の digest の上位から埋める分)。既定の引数では
    今までの一覧のまま(`[dig].daily_cap = 0`)。
    予定の行には上限が無い(`[dig].daily_cap` に上限が無い)ので、可視の長さが 1 通の上限
    (DIG_BACKLOG_TEXT_MAX_CHARS)を超えるときは行の単位で複数通に分ける。見出しは 1 通目、
    注記とリプライの案内は最後の 1 通に置き、2 通目以降には続きの見出しを付ける(GH#349)。
    with_buttons なら行のボタンを、その行を載せた 1 通に付ける(他の 1 通の行のボタンは付けない。
    1 通のボタンは DIG_BACKLOG_MAX_BUTTONS 個まで)。予定の行は ⚡ 今すぐと ⏭️ 外す(GH#349)、
    残りの行は状態が DIG_NOW_STATES のときだけ ⚡ 今すぐ(GH#388)。dug_today(今日もう掘った)の
    行には今すぐを付けない。ボタンの無い 1 通はボタンなし。
    """
    states = states or {}
    lines = [f"🔖 未消化の深掘り候補({len(plan_rows) + len(rows)}件)"]
    lines += [
        f"{row['item_id']}. {DIG_STATE_MARKS['planned']} "
        f"{_linked_title(row, DIG_BACKLOG_TITLE_MAX_CHARS)}"
        for row in plan_rows
    ]
    for row in rows[:DIG_BACKLOG_MAX_ITEMS]:
        state = states.get(row["item_id"])
        mark = f"{DIG_STATE_MARKS[state]} " if state is not None else ""
        lines.append(
            f"{row['item_id']}. {mark}{_linked_title(row, DIG_BACKLOG_TITLE_MAX_CHARS)}"
        )
    if len(rows) > DIG_BACKLOG_MAX_ITEMS:
        lines.append(f"(他 {len(rows) - DIG_BACKLOG_MAX_ITEMS} 件)")
    # 夜の予定は確約ではない(GH#349。設計 06 §2)。② は新しい順なので、朝の x_signals 段が
    # 取り込む新しい 🔖 が押し出しうる。③ は朝の digest で決まるので件数だけ示す
    notes = []
    if plan_rows:
        notes.append("🗓 明朝 = キューからの予定。朝に新しい 🔖 が入ると入れ替わることがあります")
    if digest_fill > 0:
        notes.append(f"残り {digest_fill} 件は明朝のダイジェストの上位から選びます")
    if with_buttons:
        # 何をすると一覧から消えるか(GH#398)。カードが届いただけでは消えない(✅ 掘った)
        notes.append(DIG_BACKLOG_READ_NOTE)
    example_id = (plan_rows or rows)[0]["item_id"]
    hint = (
        f"↩️ このメッセージへのリプライで指定: 「{example_id} を◯◯の観点で」= "
        f"朝の深掘りに登録 / 「{example_id} はパス」= 深掘り対象から外す"
    )
    # 注記とリプライの案内は、空行で区切った 1 つの塊として最後の 1 通に置く
    lines.append("\n".join(["", *(notes + [""] if notes else []), hint]))
    # 行の添字(見出しの次から)→ その行のボタン。ボタンはその行を載せた 1 通に付ける
    buttons_at: dict[int, BacklogButtons] = {}
    if with_buttons:
        buttons_at = {
            index: BacklogButtons(
                row["item_id"], now=row["item_id"] not in dug_today, skip=True
            )
            for index, row in enumerate(plan_rows, start=1)
        }
        for index, row in enumerate(rows[:DIG_BACKLOG_MAX_ITEMS], start=len(plan_rows) + 1):
            item_id = row["item_id"]
            if states.get(item_id) in DIG_NOW_STATES and item_id not in dug_today:
                buttons_at[index] = BacklogButtons(item_id, now=True, skip=False)
    groups = pack_line_groups(
        lines,
        DIG_BACKLOG_TEXT_MAX_CHARS - visible_length(DIG_BACKLOG_CONTINUED) - 1,
        measure=visible_length,
        counted={index: buttons.count for index, buttons in buttons_at.items()},
        max_counted=DIG_BACKLOG_MAX_BUTTONS,
    )
    messages: list[tuple[str, dict | None]] = []
    for position, group in enumerate(groups):
        text = "\n".join(lines[index] for index in group)
        if position > 0:
            # 注記の塊が 2 通目以降の頭に来たときは、先頭の空行を続きの見出しで置き換える
            text = DIG_BACKLOG_CONTINUED + "\n" + text.removeprefix("\n")
        buttons = [buttons_at[index] for index in group if index in buttons_at]
        messages.append((text, backlog_keyboard(buttons) if buttons else None))
    return messages


def notify_run_failure(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    failures: list[str],
    run_id: str,
    logger: logging.Logger,
    *,
    digest_held: bool = False,
    deliver_date: str | None = None,
) -> bool:
    """朝バッチ失敗通知(設計 05 §4)。同一種別1日1通。通知自体の失敗はログのみ。

    この呼び出しで送れたときだけ True(今日は通知済み・送信の失敗は False)。材料不足の
    リプライ指定は、送れたときだけ外す(GH#337)。

    digest_held: 分析の障害で当日の digest を確定しなかった(GH#191)。同じ 1 通で、
    復旧したら同じ日の再走で届くことを知らせる。
    deliver_date: deliver 段が送ろうとした digest の日付。その配信が失敗したら、日をまたいだ
    ときに日付を指定して送り直せることを添える。翌日の再走は当日分しか送らないため(GH#247)。
    """
    today = today_jst()
    if get_state(conn, STATE_RUN_FAILURE_NOTIFIED) == today:
        return False
    guide = (
        "分析の障害で今日のダイジェストを作れませんでした。"
        "復旧したら今日のうちに `intel run` で再走すれば届きます"
        if digest_held
        else "`intel run` で再走してください"
    )
    if deliver_date is not None and "deliver" in failures:
        guide += f"。日をまたいだら `intel deliver --date {deliver_date}` で送れます"
    try:
        api.send_message(
            _require_chat_id(config),
            f"⚠️ 朝バッチ失敗(段: {', '.join(failures)} / run_id={run_id})。{guide}",
        )
        # 送信成功後に記録(失敗した日は次の再走で再通知できる)
        set_state(conn, STATE_RUN_FAILURE_NOTIFIED, today)
    except Exception as e:
        # 通知の失敗で run の終了コードを汚さない(フォールバック経路は作らない)
        logger.error(f"失敗通知の送信に失敗: {type(e).__name__}: {e}", extra={"stage": "run"})
        return False
    return True


def _followup_targets(conn: sqlite3.Connection, dig_date: str) -> list[sqlite3.Row]:
    """最新 reaction が 🔖 で、その 🔖 を dig_date(JST)に押し、followup_dig 未記録の
    アイテム(設計 04 §4)。

    押した日は reaction の created_at(UTC)を JST に直した日付で見る。signal_date は
    KPI の帰属(配信日)なので、過去の digest に後から付けた 🔖 を取りこぼす(GH#226)。
    最新 reaction は item ごとに決める(追い掛け回答の記帳 poller._record_followup と同じ)。
    """
    start, end = jst_day_bounds_utc(dig_date)
    return conn.execute(
        """
        SELECT items.id AS item_id, items.title, items.canonical_url,
               (SELECT a.title_ja FROM analyses AS a WHERE a.item_id = items.id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS title_ja
        FROM signals AS s
        JOIN items ON items.id = s.item_id
        WHERE s.kind = 'reaction' AND s.value = 'dig'
          AND s.created_at >= ? AND s.created_at < ?
          AND s.id = (
            SELECT id FROM signals AS s2
            WHERE s2.kind = 'reaction' AND s2.item_id = s.item_id
            ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1
          )
          AND NOT EXISTS (
            SELECT 1 FROM signals AS f
            WHERE f.kind = 'followup_dig' AND f.item_id = s.item_id
          )
        ORDER BY items.id
        """,
        (start, end),
    ).fetchall()


def pending_dig_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """未消化 🔖 の累計: Telegram の 🔖 反応と X ブックマーク(GH#99)を合流した
    深掘り候補一覧(GH#88)。

    - Telegram 側: 最新 reaction が 🔖 のまま(「いいえ」回答済みは残す = まだ深掘って
      いない)
    - X 側: bookmark 紐付けのある保存アイテム。Telegram の訂正(👎等)があっても
      X 上のブックマークが実在する限り候補に残す
    - どちらも深掘り実施(最新の followup_dig が yes)の記録で消える。「はい」から
      「いいえ」への訂正で一覧に戻る(GH#195)。両方に該当する item は 1 行(マーク時刻は
      新しい方)。新しいマークが上(直近の関心を先頭に)
    """
    return conn.execute(
        """
        WITH marked AS (
          SELECT s.item_id, s.created_at AS marked_at
          FROM signals AS s
          WHERE s.kind = 'reaction' AND s.value = 'dig'
            AND s.id = (
              SELECT id FROM signals AS s2
              WHERE s2.kind = 'reaction' AND s2.item_id = s.item_id
              ORDER BY s2.created_at DESC, s2.id DESC LIMIT 1
            )
          UNION ALL
          SELECT xs.item_id, MAX(xs.observed_at) AS marked_at
          FROM x_signals AS xs
          WHERE xs.kind = 'bookmark' AND xs.item_id IS NOT NULL
          GROUP BY xs.item_id
        )
        SELECT items.id AS item_id, items.title, items.canonical_url,
               (SELECT a.title_ja FROM analyses AS a WHERE a.item_id = items.id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS title_ja,
               MAX(marked.marked_at) AS marked_at
        FROM marked
        JOIN items ON items.id = marked.item_id
        WHERE (
            SELECT f.value FROM signals AS f
            WHERE f.kind = 'followup_dig' AND f.item_id = marked.item_id
            ORDER BY f.created_at DESC, f.id DESC LIMIT 1
          ) IS NOT 'yes'
        GROUP BY items.id
        ORDER BY marked_at DESC, items.id DESC
        """
    ).fetchall()


def _watchdog_problems(conn: sqlite3.Connection, config: Config, today: str) -> list[str]:
    """watchdog が知らせる問題(平文)。空 = 問題なし。"""
    problems = []
    row = conn.execute(
        "SELECT delivery_status FROM digests WHERE digest_date = ?", (today,)
    ).fetchone()
    if row is None:
        problems.append("今朝のダイジェストが未生成です。`intel run` で再走できます")
    elif row["delivery_status"] != "sent":
        # 翌日の配信は当日分しか送らないので、日をまたいだときの送り直し方も添える(GH#247)
        problems.append(
            "今朝のダイジェストが未配信です。`intel run` で再走できます"
            f"(日をまたいだら `intel deliver --date {today}`)"
        )
    # X ソースの連続失敗(設計 02 §2)。collect はソースの失敗を run の失敗にしないので、
    # ここで拾わないと誰も気づかない(GH#124)。X を止めている間は回数が進まないので見ない
    if config.x.enabled:
        problems.extend(
            f"X ソース{alert}。直すか sources.enabled=0 で止めるまで毎朝通知します"
            for alert in source_failure_alerts(conn, "x", X_FAILURE_ALERT)
        )
    return problems


def _require_chat_id(config: Config) -> int:
    if config.telegram.owner_chat_id is None:
        raise ValueError("config.toml の [telegram].owner_chat_id が未設定(設計 04 §6)")
    return config.telegram.owner_chat_id


def _is_due(now: datetime, hhmm: str) -> bool:
    hour, minute = hhmm.split(":")
    return now.timetz().replace(tzinfo=None) >= time(int(hour), int(minute))


def survey_deadline(target_date_compact: str) -> datetime:
    """遅延回答の締切: 質問対象日の翌日 12:00 JST(設計 04 §4)。"""
    target = datetime.strptime(target_date_compact, "%Y%m%d").replace(tzinfo=JST)
    return target.replace(hour=12) + timedelta(days=1)
