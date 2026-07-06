"""受信デーモン intel poll(設計 04 §3, §5, §6 / 00 §2)。

- getUpdates long polling(50s)。offset は telegram_state に永続化し、再起動で
  取りこぼし・二重処理しない(at-least-once。signals は追記型なので重複は集計側で吸収)
- callback 処理順: ①認可(from.id)→ ②即 answerCallbackQuery(10秒失効対策)→
  ③検証(item 実在+digests 帰属)→ ④signals INSERT → ⑤editMessageReplyMarkup で ✓
- message 処理(URL 投入。設計 02 §4 / 04 §5): owner の平文メッセージから http/https
  URL を抽出し items(source=manual)へ投入して即 ACK。重複判定は canonical_url のみ
  (manual は本文が無く content_hash が空になるため 24h 内容 dedup の対象外)
- allowlist 不一致は WARN 1行(from.id・update 種別・受信時刻のみ。本文ペイロードは
  保存しない)で破棄し、応答も返さない(bot の存在を確認させない。設計 04 §6)
- 夜アンケート(`q1:`/`q2:`)と追い掛け(`f:`)は plan #19 で拡張する
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from app.config import Config
from app.db import insert_item_with_dedup
from app.delivery.telegram import DeliveryError, TelegramApi, reaction_keyboard
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
) -> None:
    """常駐ループ。失敗は即時再接続、連続失敗は30s間隔(設計 05 §8)。

    プロセス生存は launchd KeepAlive の責務。should_stop はテスト用の注入点。
    """
    if config.telegram.owner_user_id is None:
        raise DeliveryError("config.toml の [telegram].owner_user_id が未設定(設計 04 §6)")
    logger.info("poller 起動", extra={"stage": "poll"})
    while not should_stop():
        try:
            poll_once(conn, config, api, logger)
        except DeliveryError as e:
            logger.error(f"getUpdates 失敗(30s 後に再接続): {e}", extra={"stage": "poll"})
            sleep(RECONNECT_BACKOFF_S)


def poll_once(
    conn: sqlite3.Connection, config: Config, api: TelegramApi, logger: logging.Logger
) -> int:
    """getUpdates 1回分を処理し、処理した update 数を返す。"""
    offset = _load_offset(conn)
    updates = api.get_updates(offset)
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            logger.warning("update_id のない update を破棄", extra={"stage": "poll"})
            continue
        # 個別 update の処理失敗は poller を殺さず、offset を進めて次へ
        # (poison update を無限再処理しない。プロセス終了は launchd KeepAlive の責務)
        try:
            handle_update(conn, config, api, update, logger)
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
) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        _handle_callback(conn, config, api, callback, logger)
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

    parsed = _parse_reaction(callback.get("data"))
    if parsed is None:
        # 不正形式・未対応 prefix は記帳せずログのみ(設計 04 §3)。応答は返して
        # スピナーだけ止める(認可済み owner の誤操作・旧形式を無限に回さない)
        api.answer_callback_query(callback_id)
        logger.warning("未対応の callback_data を破棄", extra={"stage": "poll"})
        return
    item_id, code = parsed

    # 検証+INSERT を answer より先に行う(ローカル SQLite でミリ秒オーダー。10秒失効に
    # 対して十分速く、「DB 書込失敗時のみ ⚠️ を返す」(設計 04 §3)は answer 1回の制約上
    # 書込を先にしないと満たせない)
    recorded = False
    try:
        digest_date = _digest_date_of_item(conn, item_id)
        if digest_date is None:
            # 古いメッセージ・偽造 data を KPI 分母に入れない(設計 04 §3)
            logger.warning(
                "digests 帰属のない item への reaction を破棄",
                extra={"stage": "poll", "item_id": item_id},
            )
        else:
            conn.execute(
                "INSERT INTO signals (kind, item_id, value, signal_date) "
                "VALUES ('reaction', ?, ?, ?)",
                (item_id, _REACTION_VALUES[code], digest_date),
            )
            recorded = True
    except sqlite3.Error as e:
        # 記帳失敗を owner に伝える(⚠️)ため、ここでは握りつぶして answer に進む
        logger.error(f"signals INSERT 失敗: {type(e).__name__}: {e}", extra={"stage": "poll"})

    if not recorded:
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
        api.edit_message_reply_markup(
            chat_id, message["message_id"], reaction_keyboard(item_id, selected=code)
        )
    else:
        logger.warning(
            "message/chat の形が不正なため ✓ 反映を skip",
            extra={"stage": "poll", "item_id": item_id},
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


def _parse_reaction(data: object) -> tuple[int, str] | None:
    """`s:{item_id}:{up|dig|knw|no}` を (item_id, code) に。他形式は None。"""
    if not isinstance(data, str) or len(data.encode()) > 64:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "s" or parts[2] not in _REACTION_VALUES:
        return None
    if not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


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


def _load_offset(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM telegram_state WHERE key = ?", (OFFSET_KEY,)
    ).fetchone()
    if row is None:
        return None
    return int(row["value"])


def _save_offset(conn: sqlite3.Connection, offset: int) -> None:
    conn.execute(
        "INSERT INTO telegram_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (OFFSET_KEY, str(offset)),
    )
