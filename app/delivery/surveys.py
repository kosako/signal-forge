"""夜アンケート・🔖追い掛け・watchdog・朝バッチ失敗通知(設計 04 §4 / 05 §1.2, §4)。

- 起動は受信デーモン(intel poll)の内蔵タイマー(送信経路をポーラーに集約)。
  時刻は config([telegram].survey_time / watchdog_time。JST)
- 送信済み管理は telegram_state の日付キー。**送信前に記録する**(部分送信の再送で
  同じ質問を連打するより、失敗日は未送信 = 未回答の fail-closed 集計(PROPOSAL)に
  倒す。Telegram 送信自体のリトライは TelegramApi が3回行う)
- 通知のレート制限: 同一種別1日1通(設計 05 §4)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta

from app import source_health
from app.config import Config
from app.delivery.state import get_state, set_state
from app.delivery.telegram import (
    TITLE_MAX_CHARS,
    TelegramApi,
    escape_and_truncate,
    escape_attr,
)
from app.jst import JST, jst_day_bounds_utc, today_jst
from app.source_health import X_FAILURE_ALERT, source_failure_alerts

STATE_SURVEY_SENT = "survey_sent_date"
STATE_WATCHDOG_NOTIFIED = "watchdog_notified_date"
STATE_RUN_FAILURE_NOTIFIED = "run_failure_notified_date"
STATE_SOURCE_REPORT_SENT = "source_report_sent_date"

READ_MIN_CHOICES = ("5", "10", "15", "20", "30")  # 質問2の5択(設計 04 §4)

# 未消化 🔖 一覧(GH#88)。可視テキストを 4096 字制限に収める(URL は href 側で不算入)
DIG_BACKLOG_MAX_ITEMS = 15
DIG_BACKLOG_TITLE_MAX_CHARS = 80

# watchdog の 1 通の上限。Telegram の 4096 字に余裕を残す(超えると試し直すたびに送信が
# 失敗し、watchdog が届かない。GH#124 でソースの行が可変長になった)
WATCHDOG_TEXT_MAX_CHARS = 4000
SOURCE_REPORT_MAX_CHARS = 4000


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


def run_due_timers(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    now: datetime,
) -> None:
    """期限が来たタイマー(夜アンケート・watchdog・週 1 回のソースの健康診断)を実行する。
    poll ループから毎周期呼ぶ(失敗した後は受信と別に間を空けて試し直す。poller の
    TIMER_RETRY_S。GH#224)。

    now は JST aware。ポーラー停止中に時刻を跨いだ場合は次の起動周期で送る(遅延送信)。
    日付を跨いだ分は送らない(昨日の夜アンケートを今日送っても回答が汚れるだけ)。
    """
    today = now.strftime("%Y-%m-%d")
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
    if backlog:  # 0件の日は送らない(GH#88)
        message_id = api.send_message(chat_id, _dig_backlog_text(backlog))
        # リプライ先の認識用台帳(GH#104)。過去の一覧へのリプライも有効にするため
        # 蓄積する(REPLACE は getUpdates 再配達等での同一 id 再送に対する冪等)
        conn.execute(
            "INSERT OR REPLACE INTO dig_backlog_messages (message_id, sent_date) "
            "VALUES (?, ?)",
            (message_id, now.strftime("%Y-%m-%d")),
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
            "extra": {"followups": len(followups), "dig_backlog": len(backlog)},
        },
    )


def _linked_title(row: sqlite3.Row, budget: int) -> str:
    """タイトルを深掘り動線(canonical_url へのリンク)にする(GH#88)。"""
    title = escape_and_truncate(
        (row["title_ja"] or row["title"] or "(タイトルなし)").strip(), budget
    )
    return f'<a href="{escape_attr(row["canonical_url"])}">{title}</a>'


def _dig_backlog_text(rows: list[sqlite3.Row]) -> str:
    """未消化 🔖 一覧(1通)。新しい 🔖 順・上限超過分は件数だけ示す(GH#88)。

    番号は item id(GH#104)。表示位置の連番だと日ごとに変わり、リプライ指定
    「3664 を◯◯の観点で」が翌日以降の一覧や記憶と食い違うため、安定な id を使う。
    """
    lines = [f"🔖 未消化の深掘り候補({len(rows)}件)"]
    lines += [
        f"{row['item_id']}. {_linked_title(row, DIG_BACKLOG_TITLE_MAX_CHARS)}"
        for row in rows[:DIG_BACKLOG_MAX_ITEMS]
    ]
    if len(rows) > DIG_BACKLOG_MAX_ITEMS:
        lines.append(f"(他 {len(rows) - DIG_BACKLOG_MAX_ITEMS} 件)")
    example_id = rows[0]["item_id"]
    lines.append("")
    lines.append(
        f"↩️ このメッセージへのリプライで指定: 「{example_id} を◯◯の観点で」= "
        f"朝の深掘りに登録 / 「{example_id} はパス」= 深掘り対象から外す"
    )
    return "\n".join(lines)


def notify_run_failure(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    failures: list[str],
    run_id: str,
    logger: logging.Logger,
    *,
    digest_held: bool = False,
) -> None:
    """朝バッチ失敗通知(設計 05 §4)。同一種別1日1通。通知自体の失敗はログのみ。

    digest_held: 分析の障害で当日の digest を確定しなかった(GH#191)。同じ 1 通で、
    復旧したら同じ日の再走で届くことを知らせる。
    """
    today = today_jst()
    if get_state(conn, STATE_RUN_FAILURE_NOTIFIED) == today:
        return
    guide = (
        "分析の障害で今日のダイジェストを作れませんでした。"
        "復旧したら今日のうちに `intel run` で再走すれば届きます"
        if digest_held
        else "`intel run` で再走してください"
    )
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
        problems.append("今朝のダイジェストが未配信です。`intel run` で再走できます")
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
