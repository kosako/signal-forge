"""受信デーモン intel poll(設計 04 §3, §5, §6 / 00 §2)。

- getUpdates long polling(50s)。offset は telegram_state に永続化し、再起動で
  取りこぼし・二重処理しない(at-least-once。signals は追記型なので重複は集計側で吸収)
- callback 処理順: ①認可(from.id)→ ②検証(item 実在+digests 帰属)→
  ③signals INSERT → ④結果に応じて answerCallbackQuery(記帳成功=記録しました /
  検証失敗・期限切れ・DB 失敗など記帳できなかった場合=警告。answer は 1 callback
  1回のため記帳を先に行う。ローカル SQLite はミリ秒オーダーで10秒失効に間に合う)→
  ⑤editMessageReplyMarkup で ✓
- message 処理(URL 投入。設計 02 §4 / 04 §5): owner の平文メッセージから http/https
  URL を抽出し items(source=manual)へ投入して即 ACK。重複判定は canonical_url のみ
  (manual は本文が無く content_hash が空になるため 24h 内容 dedup の対象外)
- allowlist 不一致は WARN 1行(from.id・update 種別・受信時刻のみ。本文ペイロードは
  保存しない)で破棄し、応答も返さない(bot の存在を確認させない。設計 04 §6)
- 夜アンケート(`q1:`/`q2:`)と追い掛け(`f:`)の callback も本デーモンで処理する
  (送信は surveys.py の内蔵タイマー)
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from app.config import Config
from app.db import insert_item_with_dedup
from app.delivery.state import get_state, set_state
from app.delivery.surveys import (
    READ_MIN_CHOICES,
    followup_keyboard,
    q1_keyboard,
    q2_keyboard,
    run_due_timers,
    survey_deadline,
)
from app.delivery.telegram import DeliveryError, TelegramApi, reaction_keyboard
from app.digest.builder import JST
from app.normalize import (
    content_hash,
    is_dedup_eligible,
    normalize_text_for_hash,
    normalize_url,
)

OFFSET_KEY = "getupdates_offset"
RECONNECT_BACKOFF_S = 30  # 連続失敗時の再接続間隔(設計 05 §8)

# callback_data の code → signals.value(設計 04 §3 / 01 §7)
_REACTION_VALUES = {"up": "useful", "dig": "dig", "knw": "known", "no": "noise"}
_REACTION_EMOJI = {"up": "👍", "dig": "🔖", "knw": "✅", "no": "👎"}

_URL_RE = re.compile(r"https?://\S+")
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
) -> None:
    """常駐ループ。失敗は即時再接続、連続失敗は30s間隔(設計 05 §8)。

    各周期で内蔵タイマー(夜アンケート・watchdog。設計 05 §1.2)→ getUpdates の順に
    実行する。long poll は最大50sブロックするため、タイマーの発火粒度は約1分。
    プロセス生存は launchd KeepAlive の責務。should_stop / now_fn はテスト用の注入点。
    """
    if config.telegram.owner_user_id is None:
        raise DeliveryError("config.toml の [telegram].owner_user_id が未設定(設計 04 §6)")
    if config.telegram.owner_chat_id is None:
        # 夜アンケート・watchdog の送信先(plan #19)。起動時に fail fast
        raise DeliveryError("config.toml の [telegram].owner_chat_id が未設定(設計 04 §6)")
    logger.info("poller 起動", extra={"stage": "poll"})
    while not should_stop():
        try:
            run_due_timers(conn, config, api, logger, now_fn())
            poll_once(conn, config, api, logger, now_fn=now_fn)
        except DeliveryError as e:
            logger.error(f"getUpdates 失敗(30s 後に再接続): {e}", extra={"stage": "poll"})
            sleep(RECONNECT_BACKOFF_S)
        except sqlite3.OperationalError as e:
            # 朝バッチの VACUUM・backup と重なると busy_timeout(5s)超過で
            # "database is locked/busy" が出うる。これは一過性なので待って再開する
            # (クラッシュ→KeepAlive 再起動のループより穏当)。
            # それ以外(no such table 等のスキーマ破損)は恒久エラーなので握らず落とす
            # (KeepAlive が再起動し、doctor #3 で検知できる)
            message = str(e)
            if "locked" not in message and "busy" not in message:
                raise
            logger.error(f"DB ロック競合(30s 後に再開): {e}", extra={"stage": "poll"})
            sleep(RECONNECT_BACKOFF_S)


def poll_once(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    logger: logging.Logger,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
) -> int:
    """getUpdates 1回分を処理し、処理した update 数を返す。"""
    offset = _load_offset(conn, logger)
    updates = api.get_updates(offset)
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            logger.warning("update_id のない update を破棄", extra={"stage": "poll"})
            continue
        # 個別 update の処理失敗は poller を殺さず、offset を進めて次へ
        # (poison update を無限再処理しない。プロセス終了は launchd KeepAlive の責務)
        try:
            handle_update(conn, config, api, update, logger, now_fn=now_fn)
        except Exception as e:
            logger.error(
                f"update 処理失敗: {type(e).__name__}: {e}",
                extra={"stage": "poll"},
                exc_info=True,
            )
        _save_offset(conn, update_id + 1)
    return len(updates)


def handle_update(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    update: dict,
    logger: logging.Logger,
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _handle_callback(conn, config, api, callback, logger, now_fn=now_fn)
        return
    message = update.get("message")
    if isinstance(message, dict):
        _handle_message(conn, config, api, message, logger)
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
        api, callback, reaction_keyboard(item_id, selected=code), logger
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


def _record_followup(
    conn: sqlite3.Connection,
    api: TelegramApi,
    callback: dict,
    callback_id: str,
    item_id: int,
    code: str,
    logger: logging.Logger,
) -> None:
    """🔖追い掛け回答。signal_date は 🔖 を付けた日(設計 01 §7)。

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
        api.edit_message_reply_markup(chat_id, message["message_id"], markup)
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
    # digest の記事メッセージへのリプライ = 感想キャプチャ(GH#92)。感想の正本は
    # item_notes(Obsidian へは朝バッチ export が投影)。この経路では URL 取込をしない
    # (感想内の URL は感想の一部)。記事以外へのリプライ・非リプライは従来の URL 投入へ。
    # message_id は chat 単位のため owner DM(配信先 chat)からのリプライに限定する
    # (別チャットの同一 message_id との衝突 = 誤記帳を防ぐ。Codex must)
    if (
        isinstance(text, str)
        and text.strip()
        and chat_id == config.telegram.owner_chat_id
    ):
        note_item_id = _replied_item_id(conn, message.get("reply_to_message"))
        if note_item_id is not None:
            conn.execute(
                "INSERT INTO item_notes (item_id, note) VALUES (?, ?)",
                (note_item_id, text.strip()),
            )
            conn.commit()
            logger.info(
                "感想を記録",
                extra={"stage": "poll", "item_id": note_item_id},
            )
            reply("📝 感想を記録しました。明朝の export で Obsidian ノートに反映します")
            return

    urls = _URL_RE.findall(text) if isinstance(text, str) else []
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


def _replied_item_id(conn: sqlite3.Connection, replied: object) -> int | None:
    """リプライ先が digest の記事メッセージなら item_id を返す(GH#92)。

    外部 API 由来のネスト値は形を仮定しない(型不正は「記事へのリプライでない」扱い)。
    digest_messages は owner chat にしか送っていないため message_id 単独で一意に引ける。
    ヘッダ(item_id IS NULL)・台帳に無いメッセージ(追い掛け・アンケート等)は対象外。
    """
    if not isinstance(replied, dict):
        return None
    message_id = replied.get("message_id")
    if type(message_id) is not int:  # bool は int の派生型のため isinstance では素通りする
        return None
    row = conn.execute(
        "SELECT item_id FROM digest_messages WHERE message_id = ? AND item_id IS NOT NULL",
        (message_id,),
    ).fetchone()
    return row["item_id"] if row else None


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
