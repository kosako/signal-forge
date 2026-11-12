"""受信デーモン intel poll(設計 04 §3, §5, §6 / 00 §2)。

- getUpdates long polling(50s)。offset は telegram_state に永続化し、再起動で
  取りこぼし・二重処理しない(at-least-once。signals は追記型なので重複は集計側で吸収し、
  感想は受信したリプライの chat_id・message_id で 1 回だけ記帳する。GH#193)。受信の内容を
  DB のロック競合で保存できなかった update は offset を進めず、次の poll で再処理する(GH#181)
- callback 処理順: ①認可(from.id)→ ②検証(item 実在+digests 帰属)→
  ③signals INSERT → ④結果に応じて answerCallbackQuery(記帳成功=記録しました /
  検証失敗・期限切れ・DB 失敗など記帳できなかった場合=警告。answer は 1 callback
  1回のため記帳を先に行う。ローカル SQLite はミリ秒オーダーで10秒失効に間に合う)→
  ⑤editMessageReplyMarkup で ✓
- message 処理(URL 投入。設計 02 §4 / 04 §5): owner のメッセージから http/https
  URL を抽出し(Telegram の entities を優先し、無ければ平文を補助的に解析。GH#227)、
  items(source=manual)へ投入して即 ACK。重複判定は canonical_url のみ
  (manual は本文が無く content_hash が空になるため 24h 内容 dedup の対象外)
- allowlist 不一致は WARN 1行(from.id・update 種別・受信時刻のみ。本文ペイロードは
  保存しない)で破棄し、応答も返さない(bot の存在を確認させない。設計 04 §6)
- 夜アンケート(`q1:`/`q2:`)と追い掛け(`f:`)、未消化一覧の skip と今すぐのボタン(`b:`。
  GH#349・GH#388)、朝の digest の記事の今すぐのボタン(`g:`。GH#388)の callback も本デーモンで
  処理する(送信は surveys.py の内蔵タイマー)。今すぐの依頼は台帳に積み、深掘りは別プロセスの
  `intel dig-now` が作る(DigNowWorker)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from app.config import Config
from app.db import insert_item_with_dedup
from app.delivery.answer_worker import AnswerWorker, DigNowWorker
from app.delivery.state import get_state, set_state
from app.delivery.surveys import (
    READ_MIN_CHOICES,
    BacklogButtons,
    SentReadConfirm,
    backlog_keyboard,
    followup_keyboard,
    pending_dig_items,
    q1_keyboard,
    q2_keyboard,
    read_confirm_keyboard,
    run_due_timers,
    survey_deadline,
)
from app.delivery.telegram import (
    DeliveryError,
    TelegramApi,
    dig_card_keyboard,
    escape_and_truncate,
    pack_lines,
    reaction_keyboard,
)
from app.dig import (
    DigInstruction,
    NowRequestOutcome,
    apply_instructions,
    dig_now_marked,
    dismiss_from_queue,
    now_requests_ahead,
    parse_dig_reply,
    queue_item_ids,
    request_dig_now,
)
from app.dig_answer import (
    discussion_of_reply,
    enqueue_question,
    enqueue_request,
    question_received,
    questions_on,
    start_discussion,
)
from app.dig_take import (
    dismissed_items,
    is_dismissed,
    is_read,
    mark_dismissed,
    mark_read,
    read_items,
)
from app.export.obsidian import note_projection_date
from app.jst import JST
from app.normalize import (
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

OFFSET_KEY = "getupdates_offset"
RECONNECT_BACKOFF_S = 30  # 連続失敗時の再接続間隔(設計 05 §8)
# 受信の内容を DB の一時的な失敗(ロック競合)で保存できなかった同じ update を、やり直す回数
# (初回 + やり直し 3 回。設計 05 §8 の Telegram 送信のリトライ回数に揃える。GH#181)
TRANSIENT_RETRIES = 3
# 内蔵タイマーが失敗した後、次に試すまでの間隔(設計 05 §8。GH#224)。受信の周期とは別に数え、
# 送れない通知で Telegram を毎周期叩かない
TIMER_RETRY_S = 300

# callback_data の code → signals.value(設計 04 §3 / 01 §7)
_REACTION_VALUES = {"up": "useful", "dig": "dig", "knw": "known", "no": "noise"}
_REACTION_EMOJI = {"up": "👍", "dig": "🔖", "knw": "✅", "no": "👎"}

_URL_RE = re.compile(r"https?://\S+")
# URL の抜き出し(設計 04 §5。GH#227)。URL を終える文字 = URL に生のまま現れない `<>"` と、
# 全角の句読点・引用符。URL の直後に空白なしで続けた文(「https://…、あとで読む」)を含めない
_URL_STOP_CHARS = frozenset('<>"、。，．！？：；…“”‘’')
# 平文の URL の末尾から外す ASCII の句読点(文の区切り。URL の途中にあるものは外さない。
# url entity の範囲には当てない)
_URL_TRAILING_PUNCT = ".,;:!?'"
# 閉じ括弧 → 開き括弧(ASCII と全角)。対になっていない閉じ括弧の手前で URL を終える
# (「（https://…）を読む」)。Wikipedia の `Foo_(bar)` のような URL の中の対になった括弧は残す
_URL_BRACKETS = {
    ")": "(", "]": "[", "}": "{",
    "）": "（", "］": "［", "｝": "｛", "」": "「", "』": "『",
    "】": "【", "〕": "〔", "〉": "〈", "》": "《",
}
# scheme の後ろの authority(最初の `/` `?` `#` まで)。`@` を含めば userinfo 付き
_URL_AUTHORITY_RE = re.compile(r"https?://([^/?#]*)")
NO_URL_GUIDE = "URL を送ると翌朝のダイジェスト候補になります"


def run_poller(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    *,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
    now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
    answer_worker: AnswerWorker | None = None,
    dig_now_worker: DigNowWorker | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """常駐ループ。失敗は即時再接続、連続失敗は30s間隔(設計 05 §8)。

    各周期で内蔵タイマー(夜アンケート・watchdog。設計 05 §1.2)→ getUpdates の順に
    実行する。long poll は最大50sブロックするため、タイマーの発火粒度は約1分。
    タイマーが失敗しても同じ周期の getUpdates は止めず、タイマーは TIMER_RETRY_S 後に
    試し直す(GH#224)。プロセス生存は launchd KeepAlive の責務。should_stop / now_fn /
    clock はテスト用の注入点。
    """
    if config.telegram.owner_user_id is None:
        raise DeliveryError("config.toml の [telegram].owner_user_id が未設定(設計 04 §6)")
    if config.telegram.owner_chat_id is None:
        # 夜アンケート・watchdog の送信先(plan #19)。起動時に fail fast
        raise DeliveryError("config.toml の [telegram].owner_chat_id が未設定(設計 04 §6)")
    if answer_worker is None:
        answer_worker = AnswerWorker(config.config_dir)
    if dig_now_worker is None:
        dig_now_worker = DigNowWorker(config.config_dir)
    logger.info("poller 起動", extra={"stage": "poll"})
    # 一時的な失敗で再処理を待つ update の、続けて失敗した回数(周期をまたいで持つ。GH#181)
    transient_failures: dict[int, int] = {}
    # 内蔵タイマーが最後に失敗した時刻(clock の値。成功したら None。GH#224)
    timer_failed_at: float | None = None
    # 送れたのに台帳へ記録できなかった読んだのまとめ確認(周期をまたいで持つ。GH#399)
    unrecorded_read_confirms: list[SentReadConfirm] = []
    while not should_stop():
        try:
            # 質問への返事の取りこぼしを拾う(GH#126)。受信やタイマーが失敗する周期でも
            # 終わった子の回収と起動し直しを止めないよう、周期の最初に行う
            answer_worker.sweep(conn, logger)
            # 今すぐの深掘りも同じ(GH#388)。朝バッチの dig 段の間に押された依頼は、段が終わった
            # 後の周期で作る
            dig_now_worker.sweep(conn, logger)
            if timer_failed_at is None or clock() - timer_failed_at >= TIMER_RETRY_S:
                timer_ok = _run_timers(
                    conn, config, api, logger, now_fn(), unrecorded_read_confirms
                )
                timer_failed_at = None if timer_ok else clock()
            poll_once(
                conn, config, api, logger, now_fn=now_fn, answer_worker=answer_worker,
                dig_now_worker=dig_now_worker, transient_failures=transient_failures,
            )
        except DeliveryError as e:
            logger.error(f"getUpdates 失敗(30s 後に再接続): {e}", extra={"stage": "poll"})
            sleep(RECONNECT_BACKOFF_S)
        except sqlite3.OperationalError as e:
            # 朝バッチの VACUUM・backup と重なると busy_timeout(5s)超過で
            # "database is locked/busy" が出うる。これは一過性なので待って再開する
            # (クラッシュ→KeepAlive 再起動のループより穏当)。
            # それ以外(no such table 等のスキーマ破損)は恒久エラーなので握らず落とす
            # (KeepAlive が再起動し、doctor #3 で検知できる)。受信の内容を保存できなかった
            # update は、poll_once が offset を進めずにここへ投げ直す(待った後に、再配達で
            # 再処理する。GH#181)
            if not _is_transient_db_error(e):
                raise
            logger.error(f"DB ロック競合(30s 後に再開): {e}", extra={"stage": "poll"})
            sleep(RECONNECT_BACKOFF_S)


def _run_timers(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    now: datetime,
    unrecorded_read_confirms: list[SentReadConfirm] | None = None,
) -> bool:
    """内蔵タイマーを実行し、成功したかを返す(GH#224)。

    送信の失敗と DB のロック競合はここで受け止め、同じ周期の受信(getUpdates)を止めない
    (通知先 chat の誤りなどで送信だけが失敗し続けても、ボタン操作や URL 投入を受け取る)。
    通知系は送れてから state を立てるので、呼び出し側が TIMER_RETRY_S 後に試し直す。
    恒久的な DB エラーは握らず投げる(run_poller が落とす)。
    """
    try:
        run_due_timers(
            conn, config, api, logger, now, unrecorded_read_confirms=unrecorded_read_confirms
        )
    except DeliveryError as e:
        logger.error(
            f"内蔵タイマーの送信失敗({TIMER_RETRY_S}s 後に再試行): {e}", extra={"stage": "poll"}
        )
        return False
    except sqlite3.OperationalError as e:
        if not _is_transient_db_error(e):
            raise
        logger.error(
            f"内蔵タイマーの DB ロック競合({TIMER_RETRY_S}s 後に再試行): {e}",
            extra={"stage": "poll"},
        )
        return False
    return True


def poll_once(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
    answer_worker: AnswerWorker | None = None,
    dig_now_worker: DigNowWorker | None = None,
    transient_failures: dict[int, int] | None = None,
) -> int:
    """getUpdates 1回分を処理し、処理した update 数を返す。

    受信の内容を DB の一時的な失敗(ロック競合)で保存できなかった update は、offset を進めずに
    例外を投げ直す(GH#181)。run_poller が待ってから同じ offset で取り直し、再配達された同じ
    update を再処理する(後ろの update も、その offset より後なので一緒に再配達される)。
    transient_failures は update_id → 続けて一時的に失敗した回数で、周期をまたいで run_poller が
    持つ。TRANSIENT_RETRIES 回やり直しても失敗する update は隔離して進める。
    """
    if transient_failures is None:
        transient_failures = {}
    offset = _load_offset(conn, logger)
    updates = api.get_updates(offset)
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            logger.warning("update_id のない update を破棄", extra={"stage": "poll"})
            continue
        # 個別 update の処理失敗は poller を殺さず、offset を進めて次へ
        # (poison update を無限再処理しない。プロセス終了は launchd KeepAlive の責務)。
        # ただし DB の一時的な失敗は、待てば保存できるので offset を進めない(GH#181)
        try:
            handle_update(
                conn, config, api, update, logger, now_fn=now_fn, answer_worker=answer_worker,
                dig_now_worker=dig_now_worker,
            )
        except Exception as e:
            if not _is_transient_db_error(e):
                logger.error(
                    f"update 処理失敗: {type(e).__name__}: {e}",
                    extra={"stage": "poll"},
                    exc_info=True,
                )
            else:
                failures = transient_failures.get(update_id, 0) + 1
                if failures <= TRANSIENT_RETRIES:
                    transient_failures.clear()  # 再処理を待つのは常に先頭の未確認の 1 件
                    transient_failures[update_id] = failures
                    raise
                # やり直しても保存できない update は隔離して進める(無限に再処理しない)。
                # 本文は残さない(設計 04 §6 と同じく、update_id と種別だけ)
                logger.warning(
                    f"一時的な保存の失敗が {failures} 回続いた update を隔離して進める: "
                    f"{type(e).__name__}: {e}",
                    extra={
                        "stage": "poll",
                        "extra": {
                            "update_id": update_id,
                            "keys": sorted(set(update) - {"update_id"}),
                        },
                    },
                )
        _save_offset(conn, update_id + 1)
        transient_failures.pop(update_id, None)
    return len(updates)


def _is_transient_db_error(e: BaseException) -> bool:
    """DB のロック競合(busy_timeout 超過の "database is locked/busy")か。待てば解ける。

    それ以外(no such table 等のスキーマ破損・制約違反)は待っても直らない。
    """
    if not isinstance(e, sqlite3.OperationalError):
        return False
    message = str(e)
    return "locked" in message or "busy" in message


def handle_update(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    update: dict,
    logger: logging.Logger,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
    answer_worker: AnswerWorker | None = None,
    dig_now_worker: DigNowWorker | None = None,
) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _handle_callback(
            conn, config, api, callback, logger, now_fn=now_fn, answer_worker=answer_worker,
            dig_now_worker=dig_now_worker,
        )
        return
    message = update.get("message")
    if isinstance(message, dict):
        _handle_message(
            conn, config, api, message, logger, now=now_fn(), answer_worker=answer_worker
        )
        return
    logger.info(
        "未対応の update 種別を破棄",
        extra={"stage": "poll", "extra": {"keys": sorted(set(update) - {"update_id"})}},
    )


def _handle_callback(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    logger: logging.Logger,
    *,
    now_fn: Callable[[], datetime],
    answer_worker: AnswerWorker | None = None,
    dig_now_worker: DigNowWorker | None = None,
) -> None:
    # 外部 API 由来のネスト値は形を仮定しない(型不正は未認可と同じ扱いで破棄)
    sender = callback.get("from")
    from_id = sender.get("id") if isinstance(sender, dict) else None
    if not isinstance(from_id, int) or from_id != config.telegram.owner_user_id:
        # 本文ペイロード・raw な異常値は書かない(untrusted data をログに拡散しない。
        # 設計 04 §6)。from_id は int 検証済みのときだけ記録する
        logger.warning(
            "allowlist 外の callback を破棄",
            extra={
                "stage": "poll",
                "extra": {
                    "from_id": from_id if isinstance(from_id, int) else None,
                    "from_type": type(from_id).__name__,
                    "kind": "callback_query",
                },
            },
        )
        return

    callback_id = callback.get("id")
    if not isinstance(callback_id, str):
        logger.warning("id のない callback を破棄", extra={"stage": "poll"})
        return

    # callback_data 種別のディスパッチ(形式の正本は設計 04 §3。64バイト以内・3要素)
    data = callback.get("data")
    parts = data.split(":") if isinstance(data, str) and len(data.encode()) <= 64 else []
    if len(parts) == 3:
        prefix, arg, code = parts
        if prefix == "s" and arg.isdigit() and code in _REACTION_VALUES:
            _record_reaction(conn, api, callback, callback_id, int(arg), code, logger)
            return
        survey_value = _survey_value(prefix, code) if prefix in ("q1", "q2") else None
        if survey_value is not None and re.fullmatch(r"\d{8}", arg):
            _record_survey(
                conn, api, callback, callback_id, prefix, arg, code, survey_value,
                logger, now=now_fn(),
            )
            return
        if prefix == "f" and arg.isdigit() and code in ("y", "n"):
            _record_followup(conn, api, callback, callback_id, int(arg), code, logger)
            return
        if prefix == "d" and arg.isdigit() and code in _DIG_REQUESTS:
            _record_dig_request(
                conn, config, api, callback, callback_id, int(arg), code, logger,
                now_fn(), answer_worker,
            )
            return
        if prefix == "d" and arg.isdigit() and code == "t":
            _record_dig_discuss(conn, config, api, callback, callback_id, int(arg), logger)
            return
        if prefix == "d" and arg.isdigit() and code in _DIG_READ_CODES:
            _record_dig_read(
                conn, config, api, callback, callback_id, int(arg), _DIG_READ_CODES[code],
                logger, now_fn(),
            )
            return
        if prefix == "d" and arg.isdigit() and code in _DIG_DISMISS_CODES:
            _record_dig_dismiss(
                conn, config, api, callback, callback_id, int(arg), _DIG_DISMISS_CODES[code],
                logger,
            )
            return
        if prefix == "b" and arg.isdigit() and code == "p":
            _record_backlog_skip(conn, config, api, callback, callback_id, int(arg), logger)
            return
        if prefix == "b" and arg.isdigit() and code in _BACKLOG_READ_CODES:
            _record_backlog_read(
                conn, config, api, callback, callback_id, int(arg), _BACKLOG_READ_CODES[code],
                logger, now_fn(),
            )
            return
        if prefix == "b" and arg.isdigit() and code in _BACKLOG_DISMISS_CODES:
            _record_backlog_dismiss(
                conn, config, api, callback, callback_id, int(arg),
                _BACKLOG_DISMISS_CODES[code], logger,
            )
            return
        if prefix == "c" and (
            (arg.isdigit() and code in _READ_CONFIRM_CODES) or (arg == "0" and code == "a")
        ):
            _record_read_confirm(conn, config, api, callback, callback_id, arg, code, logger,
                                 now_fn())
            return
        if prefix == "b" and arg.isdigit() and code == "n":
            _record_backlog_now(
                conn, config, api, callback, callback_id, int(arg), logger, now_fn(),
                dig_now_worker,
            )
            return
        if prefix == "g" and arg.isdigit() and code == "n":
            _record_digest_now(
                conn, config, api, callback, callback_id, int(arg), logger, now_fn(),
                dig_now_worker,
            )
            return
    # 不正形式・未対応 prefix は記帳せずログのみ(設計 04 §3)。応答は返して
    # スピナーだけ止める(認可済み owner の誤操作・旧形式を無限に回さない)
    api.answer_callback_query(callback_id)
    logger.warning("未対応の callback_data を破棄", extra={"stage": "poll"})


def _record_reaction(
    conn: sqlite3.Connection,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    code: str,
    logger: logging.Logger,
) -> None:
    # 検証+INSERT を answer より先に行う(ローカル SQLite でミリ秒オーダー。10秒失効に
    # 対して十分速く、「記帳できたら記録しました / 記帳できなければ警告」を answer 1回の
    # 制約(設計 04 §3)で満たすには書込を先にする必要がある)
    digest_date = _digest_date_of_item(conn, item_id)
    if digest_date is None:
        # 古いメッセージ・偽造 data を KPI 分母に入れない(設計 04 §3)
        logger.warning(
            "digests 帰属のない item への reaction を破棄",
            extra={"stage": "poll", "item_id": item_id},
        )
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    if not _insert_signal(conn, "reaction", item_id, _REACTION_VALUES[code], digest_date, logger):
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return

    api.answer_callback_query(callback_id, text=f"{_REACTION_EMOJI[code]} 記録しました")
    logger.info(
        "reaction 記録",
        extra={
            "stage": "poll",
            "item_id": item_id,
            "extra": {"value": _REACTION_VALUES[code], "signal_date": digest_date},
        },
    )
    _edit_markup_best_effort(
        api, callback, _digest_item_keyboard(conn, callback, item_id, selected=code), logger
    )


def _survey_value(prefix: str, code: str) -> str | None:
    """q1/q2 の code を signals.value に写像(不正は None)。"""
    if prefix == "q1":
        return {"y": "yes", "n": "no"}.get(code)
    return code if code in READ_MIN_CHOICES else None


def _record_survey(
    conn: sqlite3.Connection,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    prefix: str,
    date_compact: str,
    code: str,
    value: str,
    logger: logging.Logger,
    *,
    now: datetime,
) -> None:
    """夜アンケート回答。callback_data 内の日付に帰属(送信日時に依存しない)。"""
    try:
        deadline = survey_deadline(date_compact)
    except ValueError:
        api.answer_callback_query(callback_id)
        logger.warning("callback_data の日付が不正", extra={"stage": "poll"})
        return
    if now > deadline:
        # 翌日 12:00 JST を過ぎた回答は記帳しない(設計 04 §4)
        api.answer_callback_query(callback_id, text="⚠️ 回答期限(翌日12:00)を過ぎています")
        logger.warning("期限切れの survey 回答を破棄", extra={"stage": "poll"})
        return

    signal_date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    kind = "survey_patrol" if prefix == "q1" else "survey_read_min"
    if not _insert_signal(conn, kind, None, value, signal_date, logger):
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return

    api.answer_callback_query(callback_id, text="📝 記録しました")
    logger.info(
        "survey 記録",
        extra={"stage": "poll", "extra": {"kind": kind, "signal_date": signal_date}},
    )
    keyboard = (
        q1_keyboard(date_compact, selected=code)
        if prefix == "q1"
        else q2_keyboard(date_compact, selected=code)
    )
    _edit_markup_best_effort(api, callback, keyboard, logger)


# 深掘りカードのボタン(GH#141)の code → 台帳の request と、受け付けたときの知らせ
_DIG_REQUESTS = {
    "s": ("summary", "📝 要約を作っています(数分かかることがあります)"),
    "m": ("more", "📖 詳しい説明を作っています(数分かかることがあります)"),
}


def _record_dig_request(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    dig_result_id: int,
    code: str,
    logger: logging.Logger,
    now: datetime,
    answer_worker: AnswerWorker | None,
) -> None:
    """深掘りカードの 📝 要約・📖 詳しく(GH#141)。依頼を台帳に積み、返事は `intel answer` が
    カードへのリプライで送る(質問と同じ流れ・同じ 1 日の上限)。新しいカードには要約を付けない
    (GH#403)が、既に送ったカードの要約のボタンは今までどおり受け付ける。

    押されたメッセージが、そのカードとして送ったメッセージであることを確かめる(callback_data
    だけを信じない)。同じカードの同じボタンは 1 回だけ受け付ける(再配達・二重押し)。
    """
    request, accepted_text = _DIG_REQUESTS[code]
    card = _pressed_dig_card(conn, config, callback, dig_result_id)
    if card is None:
        logger.warning("カードと結び付かない深掘りのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ このカードには対応できません")
        return
    log_extra = {"stage": "poll", "item_id": card["item_id"], "extra": {"request": request}}
    cap = config.dig.answer_daily_cap
    if cap <= 0:
        api.answer_callback_query(callback_id, text="返事をしない設定です")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            requested = conn.execute(
                "SELECT 1 FROM dig_questions WHERE dig_result_id = ? AND request = ?",
                (dig_result_id, request),
            ).fetchone()
            today = now.astimezone(JST).date().isoformat()
            if requested is None and questions_on(conn, today) >= cap:
                conn.execute("ROLLBACK")
                logger.warning(f"質問の上限({cap} 件/日)に達したため受け付けない", extra=log_extra)
                api.answer_callback_query(
                    callback_id, text=f"今日の返事の上限({cap} 件)に達しました"
                )
                return
            accepted = requested is None and enqueue_request(conn, dig_result_id, request)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(f"深掘りのボタンを記帳できなかった: {type(e).__name__}: {e}", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ 受け付けに失敗、もう一度")
        return
    if not accepted:
        logger.info("受け付け済みの深掘りのボタン(再配達・二重押し)", extra=log_extra)
        api.answer_callback_query(callback_id, text="受け付け済みです(返事は届いているか作成中)")
        return
    logger.info("深掘りのボタンを受け付けた", extra=log_extra)
    api.answer_callback_query(callback_id, text=accepted_text)
    if answer_worker is not None:
        answer_worker.kick(logger)


def _pressed_dig_card(
    conn: sqlite3.Connection, config: Config, callback: dict, dig_result_id: int
) -> sqlite3.Row | None:
    """押されたメッセージが、その深掘りカードとして owner の chat に送ったメッセージなら、その行。

    callback_data だけを信じない(GH#141)。行は item_id と、盤面を作り直すための canonical_url。
    """
    message = callback.get("message")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if chat_id != config.telegram.owner_chat_id:
        return None
    return conn.execute(
        "SELECT r.item_id, i.canonical_url FROM dig_results r "
        "JOIN items i ON i.id = r.item_id WHERE r.id = ? AND r.message_id = ?",
        (dig_result_id, message_id if type(message_id) is int else None),
    ).fetchone()


# 深掘りカードの「💬 話す」(GH#407)の answer。開いた / もう開いている
_DIG_DISCUSS_ANSWERS = {
    True: (
        "💬 議論を始めました。カードか返事にリプライしてください。"
        "終えるときは「まとめて」と送ってください"
    ),
    False: "💬 議論中です。リプライで続け、終えるときは「まとめて」と送ってください",
}


def _record_dig_discuss(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    dig_result_id: int,
    logger: logging.Logger,
) -> None:
    """深掘りカードの「💬 話す」(GH#407)。そのカードで議論を開く(返事はまだ作らない)。

    押されたメッセージがそのカードであることを確かめる(読んだのボタンと同じ)。1 枚のカードで
    同時に開く議論は 1 つ(再配達・二重押しで 2 つ開かない)。返事をしない設定では開かない
    (議論の返事を作れないため)。開いた後のカードと返事へのリプライは、議論の一手になる
    (_handle_dig_card_reply)。盤面は変えない(議論は「まとめて」を受け付けた poller が閉じるが、
    まとめを作れなかったときに開き直すのは別のプロセスの `intel answer` なので、ボタンの表示を
    議論の状態に合わせて保てないため)。
    """
    card = _pressed_dig_card(conn, config, callback, dig_result_id)
    if card is None:
        logger.warning("カードと結び付かない話すのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ このカードには対応できません")
        return
    log_extra = {"stage": "poll", "item_id": card["item_id"]}
    if config.dig.answer_daily_cap <= 0:
        api.answer_callback_query(callback_id, text="返事をしない設定です")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            started = start_discussion(conn, dig_result_id)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(f"議論を開けなかった: {type(e).__name__}: {e}", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info("議論を開いた" if started else "議論は開いている(書かない)", extra=log_extra)
    api.answer_callback_query(callback_id, text=_DIG_DISCUSS_ANSWERS[started])


# 深掘りカードの読んだのボタン(GH#398)の code → 読んだにするか(False = 取り消す)
_DIG_READ_CODES = {"r": True, "u": False}
# 読んだのボタンの answer。(読んだにするか, 書いたか) で決める
_DIG_READ_ANSWERS = {
    (True, True): "✅ 読んだにしました(夜の一覧から外れます)",
    (True, False): "✅ 読んだにしてあります",
    (False, True): "↩️ 読んだを取り消しました(夜の一覧に戻ります)",
    (False, False): "↩️ 読んだにはしていません",
}
# 読んだを取り消しても、不要にしてある item は一覧に戻らない(GH#403)
_DIG_READ_UNDO_DISMISSED = "↩️ 読んだを取り消しました(不要にしてあるので、一覧には載りません)"


def _record_dig_read(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    dig_result_id: int,
    read: bool,
    logger: logging.Logger,
    now: datetime,
) -> None:
    """深掘りカードの ✅ 読んだ・↩️ 読んだを取り消す(GH#398)。followup_dig を記録する。

    押されたメッセージがそのカードであることを確かめる(📝 要約・📖 詳しく と同じ)。最新の記録の
    確認と記録は 1 つのトランザクションで行い(mark_read)、再配達・二重押しで同じ値を 2 回
    書かない。盤面は押した後の状態のボタンに差し替える(読んだ ⇄ 取り消す。不要のボタンは
    今の状態のまま。GH#403)。
    """
    card = _pressed_dig_card(conn, config, callback, dig_result_id)
    if card is None:
        logger.warning("カードと結び付かない読んだのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ このカードには対応できません")
        return
    log_extra = {"stage": "poll", "item_id": card["item_id"], "extra": {"read": read}}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            written = mark_read(
                conn, card["item_id"], read, now.astimezone(JST).date().isoformat()
            )
            dismissed = is_dismissed(conn, card["item_id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(f"読んだのボタンを記録できなかった: {type(e).__name__}: {e}", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info(
        "読んだのボタンを記録した" if written else "読んだのボタン(記録済み。書かない)",
        extra=log_extra,
    )
    answer = (
        _DIG_READ_UNDO_DISMISSED
        if (read, written, dismissed) == (False, True, True)
        else _DIG_READ_ANSWERS[(read, written)]
    )
    api.answer_callback_query(callback_id, text=answer)
    keyboard = dig_card_keyboard(
        dig_result_id,
        card["canonical_url"],
        with_requests=config.dig.answer_daily_cap > 0,
        read=read,
        dismissed=dismissed,
    )
    _edit_markup_best_effort(api, callback, keyboard, logger)


# 深掘りカードの不要のボタン(GH#403)の code → 不要にするか(False = 取り消す)
_DIG_DISMISS_CODES = {"x": True, "y": False}
# 不要のボタンの answer。(不要にするか, 書いたか) で決める。一覧に載っていない item(🔖 も
# ブックマークも無い深掘り)もあるので、「外れます」ではなく「載りません」と言う
_DIG_DISMISS_ANSWERS = {
    (True, True): "👎 不要にしました(夜の一覧に載りません)",
    (True, False): "👎 不要にしてあります",
    (False, True): "↩️ 不要を取り消しました",
    (False, False): "↩️ 不要にはしていません",
}
# 調べ直しを頼んでキューに戻っていた item を不要にしたとき(深掘りの予定も外す)
_DIG_DISMISS_PASSED_ANSWERS = {
    True: "👎 不要にしました(夜の一覧に載りません。深掘りの予定も外しました)",
    False: "👎 不要にしてあります(深掘りの予定は外しました)",
}


def _record_dig_dismiss(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    dig_result_id: int,
    dismissed: bool,
    logger: logging.Logger,
) -> None:
    """深掘りカードの 👎 不要・↩️ 不要を取り消す(GH#403)。台帳 dig_dismissals に記録する。

    押されたメッセージがそのカードであることを確かめる(読んだのボタンと同じ)。今の状態の確認と
    記録は 1 つのトランザクションで行い(mark_dismissed)、再配達・二重押しで 2 回書かない。
    反応(signals)には書かない(ダイジェストの 👎 とは別の操作。ユーザーの判断)。盤面は押した後の
    状態のボタンに差し替える(不要 ⇄ 取り消す。読んだのボタンは今の状態のまま)。

    不要にするとき、調べ直しの依頼などでキューに戻っている(queued)item は、未消化一覧の
    「⏭️ 外す」と同じくパスにする(明朝の予定に載せて掘り直さない)。ほかの状態でも受付の順序の
    印を進め、返事を作っている間の調べ直しの依頼でキューに戻させない(dismiss_from_queue)。
    取り消してもパスと印は戻さない(掘り直すなら、また頼む)。
    """
    card = _pressed_dig_card(conn, config, callback, dig_result_id)
    if card is None:
        logger.warning("カードと結び付かない不要のボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ このカードには対応できません")
        return
    log_extra = {"stage": "poll", "item_id": card["item_id"], "extra": {"dismissed": dismissed}}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            written = mark_dismissed(conn, card["item_id"], dig_result_id, dismissed)
            passed = dismissed and dismiss_from_queue(conn, card["item_id"])
            read = is_read(conn, card["item_id"])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(f"不要のボタンを記録できなかった: {type(e).__name__}: {e}", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info(
        "不要のボタンを記録した" if written else "不要のボタン(記録済み。書かない)",
        extra={**log_extra, "extra": {"dismissed": dismissed, "passed": passed}},
    )
    answer = (
        _DIG_DISMISS_PASSED_ANSWERS[written]
        if passed
        else _DIG_DISMISS_ANSWERS[(dismissed, written)]
    )
    api.answer_callback_query(callback_id, text=answer)
    keyboard = dig_card_keyboard(
        dig_result_id,
        card["canonical_url"],
        with_requests=config.dig.answer_daily_cap > 0,
        read=read,
        dismissed=dismissed,
    )
    _edit_markup_best_effort(api, callback, keyboard, logger)


# 読んだのまとめ確認(GH#399)の行のボタンの code → 読んだにするか(False = 取り消す)。全部は c:0:a
_READ_CONFIRM_CODES = {"r": True, "u": False}


def _record_read_confirm(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    arg: str,
    code: str,
    logger: logging.Logger,
    now: datetime,
) -> None:
    """読んだのまとめ確認の ✅ {id}・全部(GH#399。設計 04 §3)。followup_dig を記録する(mark_read)。

    押されたメッセージが、まとめ確認として owner の chat に送った 1 通(read_confirm_messages)で
    あることと、行のボタンならその 1 通に載せた item であることを確かめる。最新の記録の確認と
    記録は 1 つのトランザクションで行い、再配達・二重押しで同じ値を 2 回書かない。盤面は台帳の
    item の順で、今の記録から作り直す(読んだにしてある行に ✓)。
    """
    message = callback.get("message")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    ledger = (
        conn.execute(
            "SELECT item_ids FROM read_confirm_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if type(message_id) is int and chat_id == config.telegram.owner_chat_id
        else None
    )
    item_ids = [int(item_id) for item_id in json.loads(ledger["item_ids"])] if ledger else []
    targets = item_ids if code == "a" else [int(arg)] if int(arg) in item_ids else []
    if not targets:
        logger.warning("まとめ確認と結び付かない読んだのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この確認には対応できません")
        return
    read = code != "u"
    day = now.astimezone(JST).date().isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            written = [item_id for item_id in targets if mark_read(conn, item_id, read, day)]
            now_read = read_items(conn, item_ids)
            # 不要にしてある item は、読んだを取り消しても一覧に戻らない(GH#403)
            dismissed = code == "u" and is_dismissed(conn, targets[0])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(
            f"まとめ確認の読んだを記録できなかった: {type(e).__name__}: {e}",
            extra={"stage": "poll"},
        )
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info(
        "まとめ確認の読んだを記録した",
        extra={"stage": "poll", "extra": {"code": code, "written": len(written)}},
    )
    api.answer_callback_query(
        callback_id, text=_read_confirm_answer(code, targets, written, dismissed=dismissed)
    )
    _edit_markup_best_effort(api, callback, read_confirm_keyboard(item_ids, now_read), logger)


def _read_confirm_answer(
    code: str, targets: list[int], written: list[int], *, dismissed: bool = False
) -> str:
    """まとめ確認のボタンの answer(GH#399)。夜の一覧の行の ✅ 読んだ(GH#409)も同じ文を使う。

    dismissed = 取り消した item が不要にしてある(GH#403。一覧には戻らない)。
    """
    if code == "a":
        return (
            f"✅ {len(written)} 件を読んだにしました" if written else "✅ 全部読んだにしてあります"
        )
    item_id = targets[0]
    if code == "r":
        return (
            f"✅ {item_id} を読んだにしました(夜の一覧から外れます)"
            if written
            else f"✅ {item_id} は読んだにしてあります"
        )
    if not written:
        return f"↩️ {item_id} は読んだにしていません"
    if dismissed:
        return f"↩️ {item_id} の読んだを取り消しました(不要にしてあるので、一覧には載りません)"
    return f"↩️ {item_id} の読んだを取り消しました(夜の一覧に戻ります)"


# 未消化一覧の行のボタンの callback_data(skip = p は GH#349、今すぐ = n は GH#388、読んだ = r|u と
# 不要 = x|y は GH#409。設計 04 §3)。盤面を作り直すときに、押されたメッセージのボタンから item id と
# ボタンの種類を読む
_BACKLOG_DATA_RE = re.compile(r"b:(\d+):([pnruxy])")
# ボタンの種類。読んだと不要は、押す前(r・x)と取り消し(u・y)を同じ種類として読む
_BACKLOG_KINDS = {"p": "p", "n": "n", "r": "r", "u": "r", "x": "x", "y": "x"}
# 一覧の行の読んだ・不要のボタンの code → 読んだ・不要にするか(False = 取り消す。GH#409)
_BACKLOG_READ_CODES = {"r": True, "u": False}
_BACKLOG_DISMISS_CODES = {"x": True, "y": False}


def _backlog_board(callback: dict) -> dict[int, set[str]]:
    """押された未消化一覧の 1 通のボタンから、行の item id → ボタンの種類(_BACKLOG_KINDS の値)。

    行の順(dict の挿入順)を保つ。読めないボタンは飛ばす。値は Telegram が送る押されたメッセージの
    盤面で、callback_data だけを信じないための照合と、盤面の作り直しに使う。
    """
    message = callback.get("message")
    markup = message.get("reply_markup") if isinstance(message, dict) else None
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    kinds: dict[int, set[str]] = {}
    for buttons in rows if isinstance(rows, list) else []:
        for button in buttons if isinstance(buttons, list) else []:
            data = button.get("callback_data") if isinstance(button, dict) else None
            match = _BACKLOG_DATA_RE.fullmatch(data) if isinstance(data, str) else None
            if match is not None:
                kinds.setdefault(int(match.group(1)), set()).add(_BACKLOG_KINDS[match.group(2)])
    return kinds


def _is_backlog_message(conn: sqlite3.Connection, config: Config, callback: dict) -> bool:
    """押されたメッセージが owner の chat の未消化一覧(dig_backlog_messages)か。

    callback_data だけを信じない(深掘りカードのボタンと同じ)。前の夜の一覧も受け付ける。
    """
    message = callback.get("message")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    listed = type(message_id) is int and (
        conn.execute(
            "SELECT 1 FROM dig_backlog_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        is not None
    )
    return listed and chat_id == config.telegram.owner_chat_id


def _record_backlog_skip(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    logger: logging.Logger,
) -> None:
    """未消化一覧の「⏭️ {id} を外す」(GH#349。設計 06 §2)。リプライのパスと同じ関数でキューから外す。

    押されたメッセージが未消化一覧であることを確かめる(_is_backlog_message)。
    - queued: パスにする(apply_instructions。観点・requested_at は残り、受付の順序の印も
      リプライのパスと同じく残す。GH#235)。朝の生成中でも、done 化の compare-and-set が外れて
      パスが残る(app.deepdive)。今すぐの依頼も取り消す(GH#388)
    - passed(二重押し・再配達・リプライで外した後): もう一度パスにする。キューの状態は変わらず、
      印はリプライでパスし直したときと同じく今の時点に進む
    - done(掘り終えた): 書かない。キューから外す対象が無い(調べ直しを止めるのとは別の操作)
    - キューに無い: 書かない(予定の行はキューにあるので、偽造か壊れた data)
    外した後は、押したメッセージのボタンのうち外した item に ✓ を付けた盤面に差し替える。
    """
    if not _is_backlog_message(conn, config, callback):
        logger.warning("未消化一覧と結び付かない skip のボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この一覧には対応できません")
        return
    log_extra = {"stage": "poll", "item_id": item_id}
    try:
        # 状態の確認と反映を 1 つのトランザクションにする(朝の done 化と競わない)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM dig_queue WHERE item_id = ?", (item_id,)
            ).fetchone()
            status = row["status"] if row is not None else None
            if status in ("queued", "passed"):
                apply_instructions(conn, [DigInstruction(item_id, "pass")])
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(f"skip のボタンを反映できなかった: {type(e).__name__}: {e}", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ 外せませんでした。もう一度押してください")
        return
    if status is None:
        logger.warning("キューに無い item の skip のボタンを破棄", extra=log_extra)
        api.answer_callback_query(callback_id, text="⚠️ この番号は深掘りのキューにありません")
        return
    if status == "done":
        logger.info("掘り終えた item の skip のボタン(書かない)", extra=log_extra)
        api.answer_callback_query(callback_id, text=f"✅ {item_id} はもう掘り終えています")
        return
    if status == "passed":
        logger.info("外し済みの item の skip のボタン(二重押し・再配達)", extra=log_extra)
        api.answer_callback_query(callback_id, text=f"⏭️ {item_id} は外し済みです")
    else:
        logger.info("skip のボタンで深掘りのキューから外した", extra=log_extra)
        api.answer_callback_query(
            callback_id,
            text=f"⏭️ {item_id} を外しました(戻すときは一覧へ「{item_id} も」とリプライ)",
        )
    _refresh_backlog_board(conn, api, callback, logger)


def _pressed_backlog_row(
    conn: sqlite3.Connection, config: Config, callback: dict, item_id: int, kind: str
) -> bool:
    """押されたメッセージが未消化一覧で、その 1 通の盤面に、その item のその種類のボタンがあるか。

    読んだ・不要のボタン(GH#409)は item の記録を書くので、callback_data の item id だけを信じず、
    一覧の 1 通に載せた行のボタンであることを確かめる(カードのボタンの _pressed_dig_card と
    同じ考え)。
    """
    return _is_backlog_message(conn, config, callback) and kind in _backlog_board(callback).get(
        item_id, set()
    )


def _record_backlog_read(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    read: bool,
    logger: logging.Logger,
    now: datetime,
) -> None:
    """未消化一覧の「✅ {id} を読んだ」と、その取り消し(GH#409。設計 06 §2)。

    深掘りカードの ✅ 読んだと同じ記録(mark_read = followup_dig)。押されたメッセージが一覧で、
    その行のボタンであることを確かめる(_pressed_backlog_row)。最新の記録の確認と記録は 1 つの
    トランザクションで行い、再配達・二重押しで同じ値を 2 回書かない。answer は日曜のまとめ確認と
    同じ文(_read_confirm_answer)。押した後は盤面を差し替え、読んだに ✓ を付ける(押すと取り消す)。
    """
    if not _pressed_backlog_row(conn, config, callback, item_id, "r"):
        logger.warning("未消化一覧と結び付かない読んだのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この一覧には対応できません")
        return
    log_extra = {"stage": "poll", "item_id": item_id, "extra": {"read": read}}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            written = mark_read(conn, item_id, read, now.astimezone(JST).date().isoformat())
            # 不要にしてある item は、読んだを取り消しても一覧に戻らない(GH#403)
            dismissed = not read and is_dismissed(conn, item_id)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(
            f"一覧の読んだのボタンを記録できなかった: {type(e).__name__}: {e}", extra=log_extra
        )
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info(
        "一覧の読んだのボタンを記録した" if written else "一覧の読んだのボタン(記録済み。書かない)",
        extra=log_extra,
    )
    api.answer_callback_query(
        callback_id,
        text=_read_confirm_answer(
            "r" if read else "u", [item_id], [item_id] if written else [], dismissed=dismissed
        ),
    )
    _refresh_backlog_board(conn, api, callback, logger)


def _backlog_dismiss_answer(
    item_id: int, dismissed: bool, written: bool, *, passed: bool, read: bool
) -> str:
    """未消化一覧の 👎 不要のボタンの answer(GH#409)。

    passed = キューに戻っていた item をパスにした(深掘りの予定も外した)。read = 取り消した item が
    読んだにしてある(一覧には戻らない)。
    """
    if dismissed:
        if not written:
            kept = f"👎 {item_id} は不要にしてあります"
            return kept + ("(深掘りの予定は外しました)" if passed else "")
        return f"👎 {item_id} を不要にしました(夜の一覧から外れます" + (
            "。深掘りの予定も外しました)" if passed else ")"
        )
    if not written:
        return f"↩️ {item_id} は不要にしていません"
    if read:
        return f"↩️ {item_id} の不要を取り消しました(読んだにしてあるので、一覧には載りません)"
    return f"↩️ {item_id} の不要を取り消しました(夜の一覧に戻ります)"


def _record_backlog_dismiss(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    dismissed: bool,
    logger: logging.Logger,
) -> None:
    """未消化一覧の「👎 {id} は不要」と、その取り消し(GH#409。設計 06 §2)。

    深掘りカードの 👎 不要と同じ台帳 dig_dismissals に書く(カードの列は NULL = 一覧の行から。
    カードの無い記事もある。migration 033)。反応(signals)には書かない。押されたメッセージが一覧で、
    その行のボタンであることを確かめる(_pressed_backlog_row)。今の状態の確認と記録は 1 つの
    トランザクションで行い、再配達・二重押しで 2 回書かない。不要にするとき、キューに積んである
    (queued)item はパスにして明朝の予定から外し、ほかの状態でも受付の順序の印を進める
    (dismiss_from_queue。カードの 👎 不要と同じ)。取り消してもパスと印は戻さない。押した後は
    盤面を差し替え、不要に ✓ を付ける(押すと取り消す)。
    """
    if not _pressed_backlog_row(conn, config, callback, item_id, "x"):
        logger.warning("未消化一覧と結び付かない不要のボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この一覧には対応できません")
        return
    log_extra = {"stage": "poll", "item_id": item_id, "extra": {"dismissed": dismissed}}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            written = mark_dismissed(conn, item_id, None, dismissed)
            passed = dismissed and dismiss_from_queue(conn, item_id)
            read = not dismissed and is_read(conn, item_id)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(
            f"一覧の不要のボタンを記録できなかった: {type(e).__name__}: {e}", extra=log_extra
        )
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    logger.info(
        "一覧の不要のボタンを記録した" if written else "一覧の不要のボタン(記録済み。書かない)",
        extra={**log_extra, "extra": {"dismissed": dismissed, "passed": passed}},
    )
    api.answer_callback_query(
        callback_id,
        text=_backlog_dismiss_answer(item_id, dismissed, written, passed=passed, read=read),
    )
    _refresh_backlog_board(conn, api, callback, logger)


# 今すぐのボタンの answer(GH#388。設計 04 §3)。受け付けたときだけ `intel dig-now` を起動する
_NOW_ANSWERS: dict[NowRequestOutcome, str] = {
    "accepted": "⚡ {item_id} を今すぐ掘ります。数分でカードが届きます",
    "duplicate": "⚡ {item_id} は受け付け済みです",
    "pending": "⚡ {item_id} は受け付け済みです(数分でカードが届きます)",
    "running": "⚡ {item_id} は作成中です",
    "done": "✅ {item_id} はもう掘り終えています",
    "dug_today": "🗓 {item_id} は今日もう掘りました(掘り直しは明日以降)",
    "no_body": "⚠️ {item_id} は本文が無いため掘れません",
    "no_item": "⚠️ この番号の記事が見つかりません",
}
# 前に待っている依頼があるときの answer(GH#396)。今すぐの深掘りは 1 件ずつ順に作るので、後ろの
# 依頼は前の件の分だけ待つ。前の件が無いときは _NOW_ANSWERS のまま
_NOW_QUEUED_ANSWERS: dict[NowRequestOutcome, str] = {
    "accepted": "⚡ {item_id} を受け付けました。前に {ahead} 件あり、順番に作ります"
    "({minutes} 分ほどで届きます)",
    "pending": "⚡ {item_id} は受け付け済みです(前に {ahead} 件。{minutes} 分ほどで届きます)",
}
# 届く目安の 1 件あたりの分。2026-11 の実測は 1 件 2〜4 分(下書き・別の AI のレビュー・直し)で、
# その上側に置く(目安を短く言って待たせるより、早く届く方がよい)
_NOW_MINUTES_PER_REQUEST = 4


def _now_answer(outcome: NowRequestOutcome, item_id: int, ahead: int) -> str:
    """今すぐのボタンの answer。前に待っている依頼があれば、その数と届く目安を添える(GH#396)。"""
    queued = _NOW_QUEUED_ANSWERS.get(outcome)
    if ahead > 0 and queued is not None:
        minutes = (ahead + 1) * _NOW_MINUTES_PER_REQUEST
        return queued.format(item_id=item_id, ahead=ahead, minutes=minutes)
    return _NOW_ANSWERS[outcome].format(item_id=item_id)


def _record_backlog_now(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    logger: logging.Logger,
    now: datetime,
    dig_now_worker: DigNowWorker | None,
) -> None:
    """未消化一覧の「⚡ {id} を今すぐ」(GH#388。設計 06 §2〜3)。今すぐの依頼を台帳に積む。

    押されたメッセージが未消化一覧であることを確かめる(_is_backlog_message)。受け付けは
    _accept_dig_now(朝の digest の記事のボタンと共通)。押した後は盤面を差し替え、今すぐに ✓ を
    付ける。
    """
    if not _is_backlog_message(conn, config, callback):
        logger.warning("未消化一覧と結び付かない今すぐのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この一覧には対応できません")
        return
    if _accept_dig_now(
        conn, config, api, callback_id, item_id, logger, now, dig_now_worker, from_backlog=True
    ):
        _refresh_backlog_board(conn, api, callback, logger)


def _record_digest_now(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    logger: logging.Logger,
    now: datetime,
    dig_now_worker: DigNowWorker | None,
) -> None:
    """朝の digest の記事の「⚡ 今すぐ深掘り」(GH#388。設計 06 §2〜3)。今すぐの依頼を台帳に積む。

    押されたメッセージが、その記事として送った digest のメッセージ(digest_messages。置換の前の
    世代も含む)で owner の chat にあることを確かめる(callback_data だけを信じない)。受け付けは
    未消化一覧のボタンと共通(_accept_dig_now)。未消化一覧への操作ではないので、受付の順序の印
    (GH#235)は付けない。押した後は、記事の反応の ✓ を残したまま今すぐに ✓ を付ける。
    """
    message = callback.get("message")
    message_id = message.get("message_id") if isinstance(message, dict) else None
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    listed = type(message_id) is int and (
        conn.execute(
            "SELECT 1 FROM digest_messages WHERE message_id = ? AND item_id = ?",
            (message_id, item_id),
        ).fetchone()
        is not None
    )
    if not listed or chat_id != config.telegram.owner_chat_id:
        logger.warning("digest の記事と結び付かない今すぐのボタンを破棄", extra={"stage": "poll"})
        api.answer_callback_query(callback_id, text="⚠️ この記事には対応できません")
        return
    if _accept_dig_now(
        conn, config, api, callback_id, item_id, logger, now, dig_now_worker, from_backlog=False
    ):
        _edit_markup_best_effort(
            api,
            callback,
            _digest_item_keyboard(
                conn, callback, item_id, selected=_latest_reaction_code(conn, item_id)
            ),
            logger,
        )


def _accept_dig_now(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    callback_id: str,
    item_id: int,
    logger: logging.Logger,
    now: datetime,
    dig_now_worker: DigNowWorker | None,
    *,
    from_backlog: bool,
) -> bool:
    """今すぐの依頼を受け付けて答える(GH#388)。盤面を差し替えてよければ True。

    状態の確認と反映は 1 つのトランザクションで行い(app.dig.request_dig_now)、受け付けたとき
    だけ `intel dig-now` を起動する。二重押し・再配達は受け付け済みとして答え、2 回は作らない
    (再配達は callback の id で見分け、依頼を閉じた後に届いても受け付け直さない)。深掘りの段が
    止まっている設定(`[dig].daily_cap = 0`)では受け付けない(ボタンも付かないので、前の
    メッセージのボタン)。DB に書けなかったときは警告で答え、押し直してもらう。
    """
    log_extra = {"stage": "poll", "item_id": item_id}
    if config.dig.daily_cap <= 0:
        api.answer_callback_query(callback_id, text="深掘りをしない設定です")
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            today = now.astimezone(JST).date().isoformat()
            outcome = request_dig_now(
                conn, config, item_id, today, callback_id, from_backlog=from_backlog
            )
            # 受け付けと同じトランザクションで数える(数えた後に前の件が増減しない)
            ahead = now_requests_ahead(conn, item_id) if outcome in _NOW_QUEUED_ANSWERS else 0
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as e:
        logger.error(
            f"今すぐのボタンを記帳できなかった: {type(e).__name__}: {e}", extra=log_extra
        )
        api.answer_callback_query(callback_id, text="⚠️ 受け付けに失敗、もう一度押してください")
        return False
    if outcome == "no_item":
        logger.warning("item の無い今すぐのボタンを破棄", extra=log_extra)
    else:
        logger.info(f"今すぐのボタン: {outcome}", extra=log_extra)
    api.answer_callback_query(callback_id, text=_now_answer(outcome, item_id, ahead))
    if outcome == "accepted" and dig_now_worker is not None:
        dig_now_worker.kick(logger)
    return True


# 反映した反応の値(signals.value)→ callback_data の code。digest の記事の盤面を作り直すときに使う
_REACTION_CODES = {value: code for code, value in _REACTION_VALUES.items()}


def _latest_reaction_code(conn: sqlite3.Connection, item_id: int) -> str | None:
    """その記事に最後に付けた反応の code(盤面の ✓ を残すため)。

    最新は KPI の集計と同じ規則で決める(created_at の最新、同じ秒は id の大きい方。設計 01 §7)。
    """
    row = conn.execute(
        "SELECT value FROM signals WHERE kind = 'reaction' AND item_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return _REACTION_CODES.get(row["value"]) if row is not None else None


def _digest_item_keyboard(
    conn: sqlite3.Connection, callback: dict, item_id: int, *, selected: str | None
) -> dict:
    """digest の記事の盤面を作り直す(反応の ✓ = selected、今すぐの ✓ = 今の状態。GH#388)。

    今すぐのボタンは、押されたメッセージに付いていたときだけ残す(送ったときに材料の無かった記事と、
    GH#388 より前の配信には付いていない)。Telegram の値の文言は使わず、callback_data だけを読む。
    """
    message = callback.get("message")
    markup = message.get("reply_markup") if isinstance(message, dict) else None
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    now_data = f"g:{item_id}:n"
    with_now = any(
        isinstance(button, dict) and button.get("callback_data") == now_data
        for buttons in (rows if isinstance(rows, list) else [])
        for button in (buttons if isinstance(buttons, list) else [])
    )
    return reaction_keyboard(
        item_id,
        selected=selected,
        with_now=with_now,
        now_marked=with_now and item_id in dig_now_marked(conn, [item_id]),
    )


def _refresh_backlog_board(
    conn: sqlite3.Connection, api: TelegramApi, callback: dict, logger: logging.Logger
) -> None:
    """押された未消化一覧の 1 通のボタンを、今の状態の ✓ を付けて差し替える(GH#349・GH#388・
    GH#409)。

    その 1 通にどの行を載せたかは台帳に残していないので、押されたメッセージのボタン
    (callback の message.reply_markup)から item id とボタンの種類だけを読み、盤面は自前で作り直す
    (Telegram の値の文言は使わない)。✓ はキューと記録の今の状態で付けるので(外した = passed、
    今すぐ = 依頼を受け付けた・掘り終えた、読んだ = 最新の followup_dig が yes、不要 = 台帳
    dig_dismissals に行がある)、同じ 1 通の前に押したボタンの ✓ も残る。カードやまとめ確認で
    読んだ・不要にした記事も、この 1 通を差し替えるときに ✓ が付く。本文の印(🗓 明朝)は
    書き換えない。読めなければ差し替えない。
    """
    kinds = _backlog_board(callback)
    if not kinds:
        logger.warning("一覧のボタンを読めないため盤面の差し替えを skip", extra={"stage": "poll"})
        return
    item_ids = list(kinds)
    placeholders = ", ".join("?" for _ in item_ids)
    passed = {
        row["item_id"]
        for row in conn.execute(
            "SELECT item_id FROM dig_queue "
            f"WHERE status = 'passed' AND item_id IN ({placeholders})",
            item_ids,
        )
    }
    board = [
        BacklogButtons(
            item_id, now="n" in found, skip="p" in found, read="r" in found, dismiss="x" in found
        )
        for item_id, found in kinds.items()
    ]
    _edit_markup_best_effort(
        api,
        callback,
        backlog_keyboard(
            board,
            passed=passed,
            now_marked=dig_now_marked(conn, item_ids),
            read=read_items(conn, item_ids),
            dismissed=dismissed_items(conn, item_ids),
        ),
        logger,
    )


def _record_followup(
    conn: sqlite3.Connection,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    code: str,
    logger: logging.Logger,
) -> None:
    """🔖追い掛け回答。signal_date は 🔖 の reaction と同じ配信日(KPI の帰属。設計 01 §7)。

    item の**最新 reaction が dig のときだけ**記帳する(タイブレークは送信側と同一)。
    dig の後に 👎 等へ訂正された item は 🔖 対象でなくなるため、古い追い掛けボタン
    からの回答を過去の dig 日へフォールバックさせない(followup_dig 率の汚染防止)。
    """
    latest = conn.execute(
        """
        SELECT value, signal_date FROM signals
        WHERE kind = 'reaction' AND item_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    if latest is None or latest["value"] != "dig":
        # 🔖 が付いていない(または訂正で外れた)item への追い掛け回答は破棄
        logger.warning(
            "🔖 状態でない item への followup を破棄",
            extra={"stage": "poll", "item_id": item_id},
        )
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return
    value = {"y": "yes", "n": "no"}[code]
    if not _insert_signal(conn, "followup_dig", item_id, value, latest["signal_date"], logger):
        api.answer_callback_query(callback_id, text="⚠️ 記録失敗、もう一度")
        return

    api.answer_callback_query(callback_id, text="🔖 記録しました")
    logger.info(
        "followup_dig 記録",
        extra={
            "stage": "poll",
            "item_id": item_id,
            "extra": {"value": value, "signal_date": latest["signal_date"]},
        },
    )
    _edit_markup_best_effort(api, callback, followup_keyboard(item_id, selected=code), logger)


def _insert_signal(
    conn: sqlite3.Connection,
    kind: str,
    item_id: int | None,
    value: str,
    signal_date: str,
    logger: logging.Logger,
) -> bool:
    try:
        conn.execute(
            "INSERT INTO signals (kind, item_id, value, signal_date) VALUES (?, ?, ?, ?)",
            (kind, item_id, value, signal_date),
        )
        return True
    except sqlite3.Error as e:
        # 記帳失敗を owner に伝える(⚠️)ため、ここでは握りつぶして answer 側で分岐する
        logger.error(f"signals INSERT 失敗: {type(e).__name__}: {e}", extra={"stage": "poll"})
        return False


def _edit_markup_best_effort(
    api: TelegramApi, callback: dict, markup: dict, logger: logging.Logger
) -> None:
    # ✓ 反映は best-effort: chat/message の形が不正でも記帳と answer は完了済みなので
    # edit だけ skip する(型不正で落とすと offset 前に例外 → 再処理で二重記帳になる)
    message = callback.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if (
        isinstance(message, dict)
        and isinstance(message.get("message_id"), int)
        and isinstance(chat_id, int)
    ):
        try:
            api.edit_message_reply_markup(chat_id, message["message_id"], markup)
        except DeliveryError as e:
            # 同一ボタンの再押下は markup 不変で Telegram が 400「message is not
            # modified」を返す。冪等な再押下は成功扱いにする(GH#55)
            if "message is not modified" not in str(e):
                raise
            logger.info(
                "markup 不変のため ✓ 反映を skip(同一ボタン再押下)",
                extra={"stage": "poll"},
            )
    else:
        logger.warning(
            "message/chat の形が不正なため ✓ 反映を skip", extra={"stage": "poll"}
        )


def _handle_message(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    message: dict,
    logger: logging.Logger,
    *,
    now: datetime,
    answer_worker: AnswerWorker | None = None,
) -> None:
    """owner メッセージからの URL 投入(設計 02 §4 / 04 §5)。"""
    sender = message.get("from")
    from_id = sender.get("id") if isinstance(sender, dict) else None
    if not isinstance(from_id, int) or from_id != config.telegram.owner_user_id:
        logger.warning(
            "allowlist 外の message を破棄",
            extra={
                "stage": "poll",
                "extra": {
                    "from_id": from_id if isinstance(from_id, int) else None,
                    "from_type": type(from_id).__name__,
                    "kind": "message",
                },
            },
        )
        return

    # 返信先は受信メッセージの chat(owner の DM。from.id 認可済み)。形不正なら返信なし
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None

    def reply(text: str) -> None:
        if isinstance(chat_id, int):
            api.send_message(chat_id, text)
        else:
            logger.warning("chat の形が不正なため返信を skip", extra={"stage": "poll"})

    text = message.get("text")
    # digest メッセージへのリプライ = 感想キャプチャ(記事 = GH#92 / ヘッダ = GH#96)。
    # 感想の正本は item_notes / digest_notes(Obsidian へは朝バッチ export が投影)。
    # この経路では URL 取込をしない(感想内の URL は感想の一部)。台帳に無いメッセージへの
    # リプライ・非リプライは従来の URL 投入へ。message_id は chat 単位のため owner DM
    # (配信先 chat)からのリプライに限定する(別チャットの同一 message_id との衝突 =
    # 誤記帳を防ぐ。Codex must)
    if (
        isinstance(text, str)
        and text.strip()
        and chat_id == config.telegram.owner_chat_id
    ):
        # 深掘りカード・その返事へのリプライ = 質問・感想(GH#126。take の記録は GH#106)
        thread = _replied_dig_thread(conn, message.get("reply_to_message"))
        if thread is not None:
            _handle_dig_card_reply(
                conn, config, thread, message, chat_id, text.strip(), reply, logger, now,
                answer_worker,
            )
            return
        note_item_id = _replied_item_id(conn, message.get("reply_to_message"))
        if note_item_id is not None:
            recorded = _insert_note(
                conn, _ITEM_NOTE_SQL, note_item_id, text.strip(), chat_id, message
            )
            conn.commit()
            if not recorded:
                logger.info(
                    "記録済みの感想の再配達を破棄", extra={"stage": "poll", "item_id": note_item_id}
                )
                return
            logger.info(
                "感想を記録",
                extra={"stage": "poll", "item_id": note_item_id},
            )
            reply("📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
            return
        note_digest_id = _replied_digest_id(conn, message.get("reply_to_message"))
        if note_digest_id is not None:
            recorded = _insert_note(
                conn, _DIGEST_NOTE_SQL, note_digest_id, text.strip(), chat_id, message
            )
            conn.commit()
            if not recorded:
                logger.info("記録済みのダイジェスト感想の再配達を破棄", extra={"stage": "poll"})
                return
            logger.info("ダイジェスト感想を記録", extra={"stage": "poll"})
            reply(
                "📝 ダイジェストへの感想を記録しました。明朝の export で Daily ノートに反映します"
            )
            return
        # 未消化 🔖 一覧へのリプライ = 深掘り指定(GH#104 / 設計 06 §2)。
        # 感想ルートと同じく owner DM 限定(message_id は chat 単位のため)
        if _replied_dig_backlog(conn, message.get("reply_to_message")):
            _handle_dig_reply(conn, text.strip(), chat_id, message, reply, logger)
            return

    urls = _extract_urls(text, message.get("entities")) if isinstance(text, str) else []
    if not urls:
        reply(NO_URL_GUIDE)
        return

    inserted, known = _ingest_manual_urls(conn, urls, logger)
    if inserted:
        note = f"(ほか{known}件は投入済み)" if known else ""
        reply(f"✅ {inserted}件受け付けました。明朝のダイジェストで分析します{note}")
    elif known:
        reply(f"投入済みです({known}件)")
    else:
        # 抽出はされたが全件が正規化 reject(userinfo・不正ポート・host なし等)
        reply("有効な URL がありませんでした")


def _extract_urls(text: str, entities: object) -> list[str]:
    """メッセージから投入する http/https の URL を出現順に抜き出す(設計 04 §5。GH#227)。

    Telegram の entities の url(本文中の URL の範囲)と text_link(文字列に付けたリンクの
    url)を優先する。entities から http/https の URL が 1 つも得られないとき(entities が
    無い・形が不正など)だけ、平文を正規表現で補助的に解析する。
    """
    urls = _entity_urls(text, entities)
    if urls:
        return urls
    candidates = (_trim_url(match) for match in _URL_RE.findall(text))
    return [url for url in candidates if _URL_RE.fullmatch(url)]


def _entity_urls(text: str, entities: object) -> list[str]:
    if not isinstance(entities, list):
        return []
    # entity の offset・length は UTF-16 の code unit で数える(Telegram Bot API)
    encoded = text.encode("utf-16-le", "surrogatepass")
    urls: list[str] = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") not in ("url", "text_link"):
            continue
        # 範囲が本文の外を指す・surrogate pair の途中で切れる entity は使わない
        span = _utf16_span(encoded, entity.get("offset"), entity.get("length"))
        if span is None:
            continue
        # text_link は文字列に付けたリンクの url をそのまま使う。url は本文の範囲を、Telegram の
        # 範囲が全角の句読点・括弧まで含む場合に備えて、URL を終える文字と対になっていない閉じ
        # 括弧の手前で切る。末尾の ASCII の句読点は、Telegram が URL の一部として範囲に含めた
        # 文字なので外さない(`https://example.com/report!` の `!`)
        if entity["type"] == "text_link":
            url = entity.get("url")
        else:
            url = _trim_url(span, strip_trailing_punct=False)
        if isinstance(url, str) and _URL_RE.fullmatch(url):
            urls.append(url)
    return urls


def _utf16_span(encoded: bytes, offset: object, length: object) -> str | None:
    """UTF-16LE に直した本文から entity の範囲を取り出す。範囲が不正なら None。"""
    if type(offset) is not int or type(length) is not int:  # bool は int の派生型のため除く
        return None
    if offset < 0 or length <= 0 or (offset + length) * 2 > len(encoded):
        return None
    try:
        return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")
    except UnicodeDecodeError:  # surrogate pair の途中で切れた範囲
        return None


def _trim_url(candidate: str, *, strip_trailing_punct: bool = True) -> str:
    """URL の候補から、URL の後ろに続けて書いた文字を外す(GH#227)。

    URL を終える文字(_URL_STOP_CHARS)か、対になっていない閉じ括弧の手前で切り、
    strip_trailing_punct なら(平文の解析)末尾の ASCII の句読点を外す。URL の中の対になった
    括弧は残す。正規化(normalize_url)の規則は変えない。
    """
    authority = _URL_AUTHORITY_RE.match(candidate)
    if authority is not None and "@" in authority.group(1):
        # userinfo 付きの URL は切らずに渡し、正規化で拒否させる(`https://a:1)@host` を `)` で
        # 切ると、資格情報の一部が host・port として通ってしまう)
        return candidate
    depth = dict.fromkeys(_URL_BRACKETS.values(), 0)
    end = len(candidate)
    for i, ch in enumerate(candidate):
        if ch in _URL_STOP_CHARS:
            end = i
            break
        if ch in depth:
            depth[ch] += 1
        elif ch in _URL_BRACKETS:
            opener = _URL_BRACKETS[ch]
            if depth[opener] == 0:
                end = i
                break
            depth[opener] -= 1
    trimmed = candidate[:end]
    return trimmed.rstrip(_URL_TRAILING_PUNCT) if strip_trailing_punct else trimmed


def _ingest_manual_urls(
    conn: sqlite3.Connection, urls: list[str], logger: logging.Logger
) -> tuple[int, int]:
    """URL 群を items(source=manual)へ投入し (新規件数, 既存件数) を返す。

    重複判定は canonical_url のみ(INSERT OR IGNORE)。manual は本文が無く
    正規化テキストが空 = dedup_eligible False で、24h 内容 dedup の親子にならない
    (設計 01 §5〜6。空ハッシュ一致で別 URL が duplicate 化する誤爆の防止)。
    """
    source_id = conn.execute(
        "SELECT id FROM sources WHERE type = 'manual' AND name = 'manual'"
    ).fetchone()["id"]
    empty_text = normalize_text_for_hash(None, None)
    inserted = 0
    known = 0
    seen: set[str] = set()
    for url in urls:
        try:
            # 短縮 URL 展開はしない(poller の外向き HTTP を Telegram API のみに保つ。
            # 設計 01 §4 の resolver 注入を manual では使わない例外 — 同 §4 に明記)
            canonical = normalize_url(url)
        except Exception as e:
            logger.warning(
                f"URL 正規化失敗のため skip: {type(e).__name__}", extra={"stage": "poll"}
            )
            continue
        if not urlsplit(canonical).hostname:
            # `https://?x=1` 等の host なし URL は normalize を素通りするため明示拒否
            logger.warning("host のない URL のため skip", extra={"stage": "poll"})
            continue
        if canonical in seen:  # 同一メッセージ内の重複は1件として扱う
            continue
        seen.add(canonical)
        outcome = insert_item_with_dedup(
            conn,
            source_id=source_id,
            external_id=None,
            url=url,
            canonical_url=canonical,
            title=None,
            author=None,
            published_at=None,
            raw_payload=None,
            content_hash=content_hash(empty_text),
            dedup_eligible=is_dedup_eligible(empty_text),
        )
        if outcome.outcome == "inserted":
            inserted += 1
            logger.info(
                "manual URL 投入", extra={"stage": "poll", "item_id": outcome.item_id}
            )
        else:
            known += 1
    return inserted, known


DIG_REPLY_GUIDE = (
    "一覧の番号で指定してください。例: 「3664 を◯◯の観点で」= 登録 / 「3664 はパス」= 除外"
)
# 深掘り指定の確認返信の長さの予算(Telegram の 1 通 4096 字に余裕を残す)。観点は 1 件
# ごとに切り詰め、行を足すと 1 通の上限を超えるなら次の 1 通に分ける(引き継いだ長い観点が
# 何件も並んでも送信が失敗しない。GH#228)。観点の上限は深掘りカードの観点の表示と揃える
DIG_ACK_ANGLE_MAX_CHARS = 300
DIG_ACK_MAX_CHARS = 3500


def _handle_dig_reply(
    conn: sqlite3.Connection,
    text: str,
    chat_id: int,
    message: dict,
    reply: Callable[[str], None],
    logger: logging.Logger,
) -> None:
    """未消化一覧へのリプライを深掘りキューへ反映し、結果を確認応答する(GH#104)。

    指定できる番号 = 未消化候補 ∪ キュー在籍(過去にキュー入りした item はパス・
    再指定のため一覧から消えていても受け付ける)。候補にない番号は本文(観点の一部)
    扱いで無視されるため、確認応答に「何が登録されたか」を必ず列挙する(黙って
    落とさない)。同じリプライの再配達は反映も確認応答もしない(GH#335)。
    """
    valid_ids = {row["item_id"] for row in pending_dig_items(conn)} | queue_item_ids(conn)
    instructions = parse_dig_reply(text, valid_ids)
    if not instructions:
        reply(DIG_REPLY_GUIDE)
        return
    # 指定はまとめて 1 つのトランザクションで反映する(接続は autocommit)。途中でロック競合に
    # なったら全部を戻し、再配達で全部をやり直す(GH#181)。反映済みの指定をやり直すと
    # requested_at が動き、生成中の深掘りの compare-and-set(deepdive)が外れて同じ依頼を
    # 翌朝も掘ってしまうため。反映の後、offset を保存する前に止まったときの再配達は、
    # リプライの記帳で見分ける(記帳と反映を同じトランザクションに入れる。GH#335)
    conn.execute("BEGIN IMMEDIATE")
    try:
        recorded = _record_dig_backlog_reply(conn, chat_id, message)
        if recorded:
            apply_instructions(conn, instructions)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if not recorded:
        logger.info("反映済みの深掘り指定の再配達を破棄", extra={"stage": "poll"})
        return
    lines = []
    for instruction in instructions:
        if instruction.action == "pass":
            lines.append(f"⏭️ {instruction.item_id} を深掘り対象から外しました")
            continue
        # 返信は parse_mode=HTML。観点は本人の自由文なので escape して埋め込む
        # (設計 04 §1。キューには原文のまま記録する。GH#194)。表示だけ上限で切る
        if instruction.angle:
            lines.append(
                f"🔍 {instruction.item_id} を登録しました"
                f"(観点: {escape_and_truncate(instruction.angle, DIG_ACK_ANGLE_MAX_CHARS)})"
            )
            continue
        # 観点を省いた再指定は前回の観点を引き継ぐ(apply_instructions の COALESCE)。
        # 入力ではなく反映後のキューの観点を出す(GH#228)
        kept = conn.execute(
            "SELECT angle FROM dig_queue WHERE item_id = ?", (instruction.item_id,)
        ).fetchone()
        if kept is not None and kept["angle"]:
            lines.append(
                f"🔍 {instruction.item_id} を登録しました"
                "(前回の観点を引き継ぎ: "
                f"{escape_and_truncate(kept['angle'], DIG_ACK_ANGLE_MAX_CHARS)})"
            )
        else:
            lines.append(f"🔍 {instruction.item_id} を登録しました(観点なし)")
    for message in pack_lines(lines, DIG_ACK_MAX_CHARS):
        reply(message)
    logger.info(
        "深掘り指定を記録",
        extra={
            "stage": "poll",
            "extra": {
                "requests": sum(1 for i in instructions if i.action == "request"),
                "passes": sum(1 for i in instructions if i.action == "pass"),
            },
        },
    )


def _record_dig_backlog_reply(conn: sqlite3.Connection, chat_id: int, message: dict) -> bool:
    """深掘りの指定を反映するリプライを記帳し、初めて受けたなら True を返す(GH#335)。

    受信したリプライの (chat_id, message_id) を冪等キーにする(migration 026 の主キー。感想の
    _insert_note と同じ形)。message_id が int でない(Telegram は必ず付ける)ときは記帳せず、
    初めての受信として扱う(キーなしで反映する)。
    """
    message_id = message.get("message_id")
    if type(message_id) is not int:  # bool は int の派生型のため除く
        return True
    return (
        conn.execute(
            "INSERT INTO dig_backlog_replies (chat_id, message_id) VALUES (?, ?) "
            "ON CONFLICT (chat_id, message_id) DO NOTHING",
            (chat_id, message_id),
        ).rowcount
        == 1
    )


def _replied_dig_backlog(conn: sqlite3.Connection, replied: object) -> bool:
    """リプライ先が未消化 🔖 一覧のメッセージか(台帳照合。GH#104)。"""
    if not isinstance(replied, dict):
        return False
    message_id = replied.get("message_id")
    if type(message_id) is not int:  # bool 除外(_replied_ledger_row と同じ理由)
        return False
    return (
        conn.execute(
            "SELECT 1 FROM dig_backlog_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        is not None
    )


def _replied_item_id(conn: sqlite3.Connection, replied: object) -> int | None:
    """リプライ先が digest の記事メッセージなら item_id を返す。

    外部 API 由来のネスト値は形を仮定しない(型不正は「記事へのリプライでない」扱い)。
    digest_messages は owner chat にしか送っていないため message_id 単独で一意に引ける。
    ヘッダは _replied_digest_id(GH#96)・深掘りカードとその返事は _replied_dig_thread
    (GH#126)・台帳に無いメッセージ(追い掛け・アンケート等)は対象外。
    """
    row = _replied_ledger_row(conn, replied, "item_id IS NOT NULL")
    return row["item_id"] if row is not None else None


def _replied_dig_thread(conn: sqlite3.Connection, replied: object) -> tuple[int, int] | None:
    """リプライ先が深掘り概要カードかその返事なら (dig_result_id, item_id) を返す(GH#126)。

    どちらも owner chat にしか送っていないため message_id 単独で引ける。返事への
    リプライは、その返事が答えたカードに帰属させる(同じカードのやり取りとして続ける)。
    """
    if not isinstance(replied, dict):
        return None
    message_id = replied.get("message_id")
    if type(message_id) is not int:  # bool は int の派生型のため isinstance では素通りする
        return None
    row = conn.execute(
        "SELECT id, item_id FROM dig_results WHERE message_id = ?", (message_id,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT r.id, r.item_id FROM dig_questions q "
            "JOIN dig_results r ON r.id = q.dig_result_id WHERE q.answer_message_id = ?",
            (message_id,),
        ).fetchone()
    return (row["id"], row["item_id"]) if row is not None else None


def _handle_dig_card_reply(
    conn: sqlite3.Connection,
    config: Config,
    thread: tuple[int, int],
    message: dict,
    chat_id: int,
    text: str,
    reply: Callable[[str], None],
    logger: logging.Logger,
    now: datetime,
    answer_worker: AnswerWorker | None,
) -> None:
    """深掘りカード・その返事へのリプライ(GH#126 / 設計 06 §6)。

    感想としては従来どおり item_notes に記録する(朝の export が Obsidian に投影するのは、
    digest に記事メッセージとして送った item だけ。設計 04 §5)。そのうえで質問として台帳に
    積み、別プロセスの `intel answer` が返事を作って送る(LLM の待ちで受信を止めない)。
    上限を超えた日・上限 0 は記録だけ。

    カードで議論を開いていれば(GH#407)、議論の一手として積む。本文が議論を終える言葉なら
    まとめとして積み、議論を閉じる。まとめは 1 日の上限で止めない(上限で議論を閉じられなく
    ならないように。上限 0 では返事を作れないので受けない)。
    """
    dig_result_id, item_id = thread
    message_id = message.get("message_id")
    if type(message_id) is int and question_received(conn, message_id):
        # 台帳に積んだ後、offset を保存する前に止まると同じ update が再配達される。
        # 受け付け済みなら何もしない(感想も返事も二重にしない)
        logger.info("受け付け済みの質問の再配達を破棄", extra={"stage": "poll", "item_id": item_id})
        return
    replied_id = message["reply_to_message"]["message_id"]
    # 感想と質問は一緒に記帳する(接続は autocommit。間で止まると、再配達で感想だけ二重になる)。
    # 議論の中かの判定も同じトランザクションで行う(別のプロセスの `intel answer` が、作れなかった
    # まとめの議論を開き直すのと競わない。GH#407)
    conn.execute("BEGIN IMMEDIATE")
    try:
        discussion = discussion_of_reply(conn, dig_result_id, text, replied_id)
        wrap = discussion is not None and discussion[1]
        cap = config.dig.answer_daily_cap
        if cap <= 0 or type(message_id) is not int:
            accept = False
            # Obsidian への反映は、export が感想を投影する item のときだけ案内する。🔖 保存節に
            # だけ載った X ブックマークのカードなど、対象外の item への感想は item_notes に残る
            # だけ(GH#229)
            notice = (
                "📝 感想を記録しました。明朝の export で Obsidian ノートに反映します"
                if note_projection_date(conn, item_id) is not None
                else "📝 感想を記録しました"
            )
        elif not wrap and questions_on(conn, now.astimezone(JST).date().isoformat()) >= cap:
            accept = False
            notice = f"📝 記録しました。今日の返事の上限({cap} 件)に達したため、返事は作りません"
            logger.warning(
                f"質問の上限({cap} 件/日)に達したため返事を作らない",
                extra={"stage": "poll", "item_id": item_id},
            )
        else:
            accept = True
            notice = (
                "🧾 議論をまとめています(数分かかることがあります)"
                if wrap
                else "💬 返事を作っています(数分かかることがあります)"
                if discussion is not None
                else "📝 記録しました。返事を作っています(数分かかることがあります)"
            )
        recorded = _insert_note(conn, _ITEM_NOTE_SQL, item_id, text, chat_id, message)
        if recorded and accept:
            # 返信先(形は _replied_dig_thread が確かめ済み)も渡す。失敗の知らせへのリプライを
            # 答えられなかった元の質問に結び付けるため(GH#236)
            enqueue_question(
                conn, dig_result_id, text, message_id, replied_id,
                discussion_id=discussion[0] if discussion is not None else None,
                wrap=wrap,
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if not recorded:
        # 質問として受け付けなかった感想(上限・上限 0)は台帳に無く、上の判定では分からない。
        # 感想の記帳で判定する(GH#193)
        logger.info("記録済みの感想の再配達を破棄", extra={"stage": "poll", "item_id": item_id})
        return
    logger.info(
        ("議論のまとめを受け付けた" if wrap else "議論の一手を受け付けた" if discussion else
         "質問を受け付けた") if accept else "感想を記録",
        extra={"stage": "poll", "item_id": item_id},
    )
    reply(notice)
    if accept and answer_worker is not None:
        answer_worker.kick(logger)


# 感想の記帳(GH#92 / GH#96)。受信したリプライの (chat_id, message_id) を冪等キーにする
# (GH#193。migration 019 の UNIQUE)
_ITEM_NOTE_SQL = (
    "INSERT INTO item_notes (item_id, note, chat_id, message_id) VALUES (?, ?, ?, ?) "
    "ON CONFLICT (chat_id, message_id) DO NOTHING"
)
_DIGEST_NOTE_SQL = (
    "INSERT INTO digest_notes (digest_id, note, chat_id, message_id) VALUES (?, ?, ?, ?) "
    "ON CONFLICT (chat_id, message_id) DO NOTHING"
)


def _insert_note(
    conn: sqlite3.Connection, sql: str, parent_id: int, note: str, chat_id: int, message: dict
) -> bool:
    """感想を記帳し、記帳したら True を返す(GH#193)。

    記帳した後、offset を保存する前に止まる(または保存に失敗する)と同じ update が再配達される。
    同じリプライを記帳済みなら記帳せずに False を返す(記帳と処理済みの判定を同じ 1 文で確定する)。
    message_id が int でない(Telegram は必ず付ける)ときは、キーなしで記帳する。
    """
    message_id = message.get("message_id")
    key = message_id if type(message_id) is int else None  # bool は int の派生型のため除く
    return conn.execute(sql, (parent_id, note, chat_id, key)).rowcount == 1


def _replied_digest_id(conn: sqlite3.Connection, replied: object) -> int | None:
    """リプライ先が digest のヘッダメッセージなら digest_id を返す(GH#96)。"""
    row = _replied_ledger_row(conn, replied, "item_id IS NULL")
    return row["digest_id"] if row else None


def _replied_ledger_row(
    conn: sqlite3.Connection, replied: object, where: str
) -> sqlite3.Row | None:
    if not isinstance(replied, dict):
        return None
    message_id = replied.get("message_id")
    if type(message_id) is not int:  # bool は int の派生型のため isinstance では素通りする
        return None
    # --force の置換で印を付けた前の世代の行(superseded_at)も引く。Telegram に残る
    # 置換の前のメッセージへのリプライも、そのメッセージの記事・digest に帰属させる(GH#190)
    return conn.execute(
        f"SELECT digest_id, item_id FROM digest_messages WHERE message_id = ? AND {where}",
        (message_id,),
    ).fetchone()


def _digest_date_of_item(conn: sqlite3.Connection, item_id: int) -> str | None:
    """item が配信された digest_date(帰属検証を兼ねる。設計 04 §3 / 01 §7)。

    items 実在と digests.item_ids 包含を同時に確認する。複数 digest に載る事は
    ない想定だが、--force 再生成を考慮し最新の digest_date を採る。
    """
    row = conn.execute(
        """
        SELECT max(digests.digest_date) AS digest_date
        FROM digests, json_each(digests.item_ids) AS je
        JOIN items ON items.id = je.value
        WHERE items.id = ?
        """,
        (item_id,),
    ).fetchone()
    return row["digest_date"]


def _load_offset(conn: sqlite3.Connection, logger: logging.Logger) -> int | None:
    value = get_state(conn, OFFSET_KEY)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        # 破損・手編集された offset で poller を落とさない。None(offset なし)で
        # 再開すると未確認 update が再配達される(at-least-once で安全側)
        logger.warning(
            f"telegram_state の offset が不正なためリセット: {value[:20]!r}",
            extra={"stage": "poll"},
        )
        return None


def _save_offset(conn: sqlite3.Connection, offset: int) -> None:
    set_state(conn, OFFSET_KEY, str(offset))
