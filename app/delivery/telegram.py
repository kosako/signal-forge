"""Telegram 配信(設計 04 §1〜2 / 05 §8)。

- Bot API は httpx 直叩き。parse_mode=HTML。エスケープ規定: テキストノードは
  `& < >`、属性値はさらに `"` `'`(canonical_url は外部制御可能なため属性破壊を防ぐ)
- 構成: ヘッダ1通+アイテム最大 `digest.max_items` 通(1アイテム=1メッセージ。
  ボタンをアイテム単位に付けるため)
- 冪等性: `digest_messages` 台帳(今の世代の行。GH#190)にある message は再送しない
  (部分再送)。全通完了で `digests.delivery_status='sent'` + `sent_at`。途中失敗は 'failed' を
  記録して例外。
  同じ DB への配信は DB の隣の `<DB>.deliver.lock`(flock。app/deliver_lock.py)で直列化する
  (GH#192)。digest の --force の置換も同じロックで配信と直列にする(GH#333)。送信できた
  message の記帳に失敗したら記帳だけをやり直し、それでも駄目なら message_id をログに残して
  止める(GH#225)
- token は `.env` の TELEGRAM_BOT_TOKEN。URL に token が入るため、httpx の例外を
  そのまま伝播させない(sanitize して再送出)
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from app.config import Config
from app.deliver_lock import delivery_lock
from app.digest.builder import BOOKMARK_SECTION_MAX
from app.models import EXTRACTION_FAILED_NOTE

TELEGRAM_API_BASE = "https://api.telegram.org"
ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"

TIMEOUT_S = 30  # 設計 05 §8
MAX_RETRIES = 3  # 初回 + リトライ3回(同上)
BACKOFF_S = (1.0, 2.0, 4.0)
LONG_POLL_S = 50  # getUpdates の long polling 秒(設計 05 §8)

# 送信できた message の記帳だけのやり直し(GH#225)。待ちは BACKOFF_S
RECORD_RETRIES = 3

ITEM_TEXT_MAX_CHARS = 1000  # 1通の上限(概算)。超過は要約側で吸収(設計 04 §2)
# タイトルは RSS 由来で長さ未検証。異常に長い1件で Telegram の 4096 字上限を超えて
# 配信全体が failed になるのを防ぐ(通常のタイトルはこの上限に届かない)
TITLE_MAX_CHARS = 300
# ソース名(sources.name)は運用者が手で入れる値で長さ未検証。タイトルと同じ理由で切る
# (通常の名前はこの上限に届かない。GH#356)
SOURCE_NAME_MAX_CHARS = 80

# 🔖 保存節(GH#99)のタイトル上限。ヘッダ 1 通に同梱するため短めに取り、可視テキストを
# 4096 字制限に収める(URL は href 側で不算入)。表示件数上限は builder と共通
BOOKMARK_TITLE_MAX_CHARS = 80

# 深掘りカードへの質問への返事(GH#126)の 1 通の上限。Telegram の 4096 字に余裕を残す
ANSWER_TEXT_MAX_CHARS = 4000

_WEEKDAYS_JA = "月火水木金土日"

# 反応ボタン(callback_data 形式の正本は設計 04 §3)
_REACTION_BUTTONS = (
    ("👍 価値あり", "up"),
    ("🔖 深掘り", "dig"),
    ("✅ 既知", "knw"),
    ("👎 不要", "no"),
)


class DeliveryError(Exception):
    """配信の失敗(設定不備・API エラー・リトライ上限)。"""


class TelegramApi:
    """Bot API の薄いラッパ。テストは transport(httpx.MockTransport)を注入する。"""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=f"{TELEGRAM_API_BASE}/bot{token}",
            transport=transport,
            trust_env=False,
            timeout=TIMEOUT_S,
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """sendMessage を実行し message_id を返す。リトライ規約は設計 05 §8。"""
        payload: dict[str, object] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        if reply_to_message_id is not None:
            # 返信先が消されていても送る(返事そのものを落とさない)
            payload["reply_parameters"] = json.dumps(
                {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
            )
        result = self._post("sendMessage", payload)
        if not isinstance(result, dict):
            raise DeliveryError(f"sendMessage の応答形が不正: {type(result).__name__}")
        return int(result["message_id"])

    def get_me(self) -> dict:
        """getMe(token 有効性検査。doctor #4)。bot 情報 dict を返す。"""
        result = self._post("getMe", {})
        if not isinstance(result, dict):
            raise DeliveryError(f"getMe の応答形が不正: {type(result).__name__}")
        return result

    def get_updates(self, offset: int | None) -> list[dict]:
        """getUpdates(long polling 50s。設計 05 §8)。update の list を返す。"""
        payload: dict[str, object] = {"timeout": LONG_POLL_S}
        if offset is not None:
            payload["offset"] = offset
        # read timeout は long poll 秒より長く取る(通常の 30s では poll 中に切れる)
        result = self._post("getUpdates", payload, timeout=LONG_POLL_S + 10)
        if not isinstance(result, list):
            raise DeliveryError(f"getUpdates の応答形が不正: {type(result).__name__}")
        return [u for u in result if isinstance(u, dict)]

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> None:
        """answerCallbackQuery(10秒失効対策の即時応答。設計 04 §3)。"""
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        """editMessageReplyMarkup(選択状態 ✓ の盤面差し替え。設計 04 §3)。"""
        self._post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps(reply_markup),
            },
        )

    def _post(
        self, method: str, payload: dict[str, object], *, timeout: float | None = None
    ) -> object:
        # timeout=None は httpx では「無制限」の意味になるため、未指定は client 既定に倒す
        request_timeout = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
        last_error = "不明"
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._client.post(f"/{method}", data=payload, timeout=request_timeout)
            except httpx.HTTPError as e:
                # httpx 例外の repr は token 入り URL を含みうるため型名だけ持ち越す
                last_error = f"transport エラー: {type(e).__name__}"
                self._backoff(attempt)
                continue
            body = self._parse_body(response)
            if body.get("ok") is True:
                # result の形は method 依存(dict / list / bool)。検証は呼び出し側
                return body.get("result")
            last_error = f"HTTP {response.status_code}: {body.get('description', '(説明なし)')}"
            if response.status_code == 429:
                self._wait_retry_after(body, attempt)
                continue
            if 500 <= response.status_code <= 599:
                self._backoff(attempt)
                continue
            # 4xx(chat 不在・パース不能等)は恒久エラー。リトライしない
            raise DeliveryError(f"Telegram {method} が失敗: {last_error}")
        raise DeliveryError(f"Telegram {method} がリトライ上限で失敗: {last_error}")

    @staticmethod
    def _parse_body(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _wait_retry_after(self, body: dict, attempt: int) -> None:
        if attempt >= MAX_RETRIES:
            return
        parameters = body.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if isinstance(retry_after, int) and retry_after >= 0:
            self._sleep(retry_after)
        else:
            self._backoff(attempt)

    def _backoff(self, attempt: int) -> None:
        if attempt < MAX_RETRIES:
            self._sleep(BACKOFF_S[attempt] + random.random())


def create_api(env: dict[str, str]) -> TelegramApi:
    """`.env` から TelegramApi を組み立てる(token 欠落は fail fast)。"""
    token = env.get(ENV_BOT_TOKEN)
    if not token:
        raise DeliveryError(f".env に {ENV_BOT_TOKEN} がない(設計 05 §6)")
    return TelegramApi(token)


def deliver_digest(
    conn: sqlite3.Connection,
    config: Config,
    api: TelegramApi,
    digest_id: int,
    logger: logging.Logger,
    *,
    run_id: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """digest を配信し、送信した message 数を返す(送信済みは 0)。

    台帳(digest_messages)に無い message のみ送る部分再送。1通送るごとに台帳へ
    コミットし、途中失敗しても送信済み分を二重送信しない。台帳の確認から送信・記帳までは
    同じ DB への配信どうしで直列化し、朝バッチと手動の `intel deliver` が重なっても、後の側は
    先の側の記帳を読んでから判断する(GH#192)。sleep は記帳のやり直しの待ち(GH#225)。
    """
    if config.telegram.owner_chat_id is None:
        raise DeliveryError("config.toml の [telegram].owner_chat_id が未設定(設計 04 §6)")
    # 置換(digest の --force)も同じロックを取るので、配信の途中で内容と台帳の世代が
    # 置き換わらない(GH#333)。ロックは記帳と完了の更新(sent / failed)まで持つ
    with delivery_lock(
        conn, logger, run_id=run_id, stage="deliver",
        wait_message="別の配信が実行中のため、終わるのを待つ",
    ):
        return _deliver_locked(
            conn, config.telegram.owner_chat_id, api, digest_id, logger, run_id, sleep
        )


def _deliver_locked(
    conn: sqlite3.Connection,
    chat_id: int,
    api: TelegramApi,
    digest_id: int,
    logger: logging.Logger,
    run_id: str | None,
    sleep: Callable[[float], None],
) -> int:
    """deliver_digest の本体(delivery_lock の中で呼ぶ)。"""
    digest = conn.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
    if digest is None:
        raise DeliveryError(f"digest id={digest_id} が存在しない")
    if digest["delivery_status"] == "sent":
        logger.info(
            "配信済みのため skip",
            extra={"run_id": run_id, "stage": "deliver"},
        )
        return 0

    item_ids: list[int] = json.loads(digest["item_ids"])
    items = _load_items(conn, item_ids)
    sent_item_ids, header_sent = _load_ledger(conn, digest_id)

    sent = 0
    try:
        if not header_sent:
            message_id = api.send_message(
                chat_id, _header_text(digest, _load_bookmark_rows(conn, digest))
            )
            _record_message(conn, digest_id, None, message_id, logger, run_id, sleep)
            sent += 1
        for position, row in enumerate(items, start=1):
            if row["item_id"] in sent_item_ids:
                continue
            message_id = api.send_message(
                chat_id,
                _item_text(position, row),
                reply_markup=reaction_keyboard(row["item_id"]),
            )
            _record_message(conn, digest_id, row["item_id"], message_id, logger, run_id, sleep)
            sent += 1
    except DeliveryError:
        conn.execute(
            "UPDATE digests SET delivery_status = 'failed' WHERE id = ?", (digest_id,)
        )
        logger.error(
            "配信が途中失敗(台帳にある分は再走で送らない。未送信のみ再送)",
            extra={"run_id": run_id, "stage": "deliver", "extra": {"sent": sent}},
        )
        raise

    conn.execute(
        "UPDATE digests SET delivery_status = 'sent', "
        "sent_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (digest_id,),
    )
    logger.info(
        "配信完了",
        extra={"run_id": run_id, "stage": "deliver", "extra": {"messages": sent}},
    )
    return sent


def is_telegram_configured(config: Config, env: dict[str, str]) -> bool:
    """deliver 段を実行すべきかの判定(朝バッチ用。設計 00 §6 の局所化と両立)。

    完全未設定(chat_id も token も無い)= 未セットアップとして False。
    片方だけの設定は設定ミスなので True を返し、deliver 段で fail fast させる。
    """
    return config.telegram.owner_chat_id is not None or bool(env.get(ENV_BOT_TOKEN))


# --- メッセージ組み立て(設計 04 §2) ---


def escape_text(value: str) -> str:
    """HTML テキストノード用エスケープ(& < > の3文字)。"""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    """HTML 属性値用エスケープ(テキストノード + " ')。"""
    return escape_text(value).replace('"', "&quot;").replace("'", "&#x27;")


def _header_text(digest: sqlite3.Row, bookmark_rows: list[sqlite3.Row] = ()) -> str:
    date = datetime.strptime(digest["digest_date"], "%Y-%m-%d")
    weekday = _WEEKDAYS_JA[date.weekday()]
    header = (
        f"📡 {digest['digest_date']}({weekday})のダイジェスト — {digest['item_count']}件"
    )
    if digest["status"] == "degraded":
        # LLM の全滅によるタイトルのみの配信。本文の抽出の失敗は記事ごとに出す(GH#240)
        header += "\n⚠️ 分析縮退あり(LLM の失敗でタイトルのみ)"
    if digest["trend_markdown"]:  # 「今日のトレンド」節を冒頭メッセージに同梱(GH#75)
        # trend はプレーンテキスト。HTML(parse_mode=HTML)へ載せる前に必ず escape する
        # (LLM 生成の <>& で配信全体が 400 で落ちるのを防ぐ)
        header += "\n\n" + escape_text(digest["trend_markdown"])
    if bookmark_rows:  # 🔖 保存節(must-include の別枠。GH#99)
        lines = [f"🔖 保存({len(bookmark_rows)}件)"]
        for position, row in enumerate(bookmark_rows[:BOOKMARK_SECTION_MAX], start=1):
            title = escape_and_truncate(
                (row["title_ja"] or row["title"] or row["canonical_url"]).strip(),
                BOOKMARK_TITLE_MAX_CHARS,
            )
            lines.append(f'{position}. <a href="{escape_attr(row["canonical_url"])}">{title}</a>')
        if len(bookmark_rows) > BOOKMARK_SECTION_MAX:
            lines.append(f"(他 {len(bookmark_rows) - BOOKMARK_SECTION_MAX} 件は Items ノートへ)")
        header += "\n\n" + "\n".join(lines)
    return header


def _load_bookmark_rows(conn: sqlite3.Connection, digest: sqlite3.Row) -> list[sqlite3.Row]:
    """digests.bookmark_item_ids の表示素材を保存順のまま返す(GH#99)。"""
    ids = json.loads(digest["bookmark_item_ids"] or "[]")
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT items.id AS item_id, items.title, items.canonical_url,
               (SELECT a.title_ja FROM analyses AS a WHERE a.item_id = items.id
                ORDER BY a.analyzed_at DESC, a.id DESC LIMIT 1) AS title_ja
        FROM items WHERE items.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    by_id = {row["item_id"]: row for row in rows}
    return [by_id[i] for i in ids if i in by_id]


def _item_text(position: int, row: sqlite3.Row) -> str:
    # 見出しは日本語タイトル優先(analyze-v002)。NULL / 空は原文に fallback
    raw_title = row["title_ja"] or row["title"] or "(タイトルなし)"
    title = escape_and_truncate(raw_title.strip(), TITLE_MAX_CHARS)
    source_line = (
        f'<a href="{escape_attr(row["canonical_url"])}">'
        f"{escape_and_truncate(row['source_name'], SOURCE_NAME_MAX_CHARS)}</a>"
    )
    published = _published_md(row["published_at"])
    if published:
        source_line += f" ・{published}"

    fixed_lines = [f"{position}. <b>{title}</b>", source_line]
    # 本文を読めず見出しだけで作った分析は、本文に基づく要約と見分けられるよう印を出す(GH#240)。
    # 切り詰め対象外の行として予算に含める。degraded は分析が無いので出さない
    if row["extraction_failed"] and not row["degraded"]:
        fixed_lines.append(EXTRACTION_FAILED_NOTE)
    # 🎯 推薦の理由と 💡 示唆の行は切り詰め対象外(設計 04 §2: タイトル・URL・理由・示唆は
    # 削らない)。degraded の理由欄は記録用の定型文(LLM 分析失敗)なので出さない(GH#90)
    reason = "" if row["degraded"] else (row["recommendation_reason"] or "").strip()
    implication = (row["implication"] or "").strip()
    trailers = []
    if reason:
        trailers.append(f"🎯 {escape_text(reason)}")
    if implication:
        trailers.append(f"💡 {escape_text(implication)}")

    summary = (row["summary"] or "").strip()
    budget = ITEM_TEXT_MAX_CHARS - sum(len(line) + 1 for line in fixed_lines + trailers)
    if summary and budget > 1:
        fixed_lines.append(escape_and_truncate(summary, budget))
    return "\n".join(fixed_lines + trailers)


def escape_and_truncate(raw: str, budget: int) -> str:
    """raw をエスケープし、エスケープ後長が budget 内に収まるよう末尾を … で切る。

    エスケープ済み文字列の任意位置 slice は HTML entity(&amp; 等)を分断し、
    Telegram の parse_mode=HTML が 400 を返しうるため、raw 1文字単位で積む。
    """
    escaped = escape_text(raw)
    if len(escaped) <= budget:
        return escaped
    parts: list[str] = []
    used = 0
    for char in raw:
        piece = escape_text(char)
        if used + len(piece) > budget - 1:  # 「…」1文字分を確保
            break
        parts.append(piece)
        used += len(piece)
    return "".join(parts) + "…"


_TAG_RE = re.compile(r"<[^>]*>")


def visible_length(html_text: str) -> int:
    """parse_mode=HTML の本文が Telegram で数えられる長さ(タグを除き entity を戻した可視の
    テキストの UTF-16 の code unit 数。href の URL は数えない)。

    本モジュールが組み立てる本文(escape_text・escape_attr を通した値と固定のタグ)が前提。
    属性値の < > は escape 済みなので、タグの範囲を > で閉じてよい。
    """
    visible = html.unescape(_TAG_RE.sub("", html_text))
    return len(visible.encode("utf-16-le", "surrogatepass")) // 2


def pack_lines(
    lines: list[str], budget: int, *, measure: Callable[[str], int] = len
) -> list[str]:
    """行を改行でつなぎ、1 通が budget を超えないように分ける(行の途中では切らない)。

    1 行が budget を超えないことは呼び出し側が保証する(観点を切り詰めて組み立てる)。
    measure は 1 通の長さの数え方(既定は文字数。可視の長さで数えるなら visible_length)。
    深掘り指定の確認返信(GH#228)と未消化一覧(GH#349)が使う。
    """
    return [
        "\n".join(lines[index] for index in group)
        for group in pack_line_groups(lines, budget, measure=measure)
    ]


def pack_line_groups(
    lines: Sequence[str],
    budget: int,
    *,
    measure: Callable[[str], int] = len,
    counted: Collection[int] = (),
    max_counted: int | None = None,
) -> list[list[int]]:
    """pack_lines の分け方で、1 通ごとの行の添字を返す。

    counted の行(添字)は 1 通に max_counted 行までにする(ボタンを付ける行の数を、1 通の
    ボタンの数の上限に収める。未消化一覧の skip のボタン。GH#349)。
    """
    groups: list[list[int]] = []
    current: list[int] = []
    used = 0
    in_current = 0
    for index, line in enumerate(lines):
        size = measure(line)
        is_counted = index in counted
        full = max_counted is not None and is_counted and in_current >= max_counted
        if current and (used + 1 + size > budget or full):
            groups.append(current)
            current, used, in_current = [], 0, 0
        used = used + 1 + size if current else size
        current.append(index)
        in_current += int(is_counted)
    if current:
        groups.append(current)
    return groups


def _published_md(published_at: str | None) -> str | None:
    if not published_at:
        return None
    try:
        date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None  # 不正な日付は日付表示だけ落とす(配信自体は止めない)
    return f"{date.month}/{date.day}"


def reaction_keyboard(item_id: int, *, selected: str | None = None) -> dict:
    """4ボタン InlineKeyboard。selected(up|dig|knw|no)のボタンに ✓ を付ける。"""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{label} ✓" if code == selected else label,
                    "callback_data": f"s:{item_id}:{code}",
                }
                for label, code in _REACTION_BUTTONS
            ]
        ]
    }


# 深掘りカードのボタン(GH#141。callback_data 形式の正本は設計 04 §3)
_DIG_REQUEST_BUTTONS = (("📝 要約", "s"), ("📖 詳しく", "m"))
_URL_BUTTON_MAX = 2048  # 送信が恒久失敗しないよう、変な長さの URL にはボタンを付けない
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def dig_card_keyboard(
    dig_result_id: int, url: str | None, *, with_requests: bool
) -> dict | None:
    """深掘りカードのボタン: 🔗 元記事(URL ボタン)/ 📝 要約 / 📖 詳しく(GH#141)。

    URL ボタンは http(s) の URL のときだけ付ける(不正な URL があると sendMessage 自体が
    400 になり、カードが届かなくなるため)。要約・詳しくは返事をする設定のときだけ付ける。
    """
    row: list[dict] = []
    if _is_button_url(url):
        row.append({"text": "🔗 元記事", "url": url})
    if with_requests:
        row.extend(
            {"text": label, "callback_data": f"d:{dig_result_id}:{code}"}
            for label, code in _DIG_REQUEST_BUTTONS
        )
    return {"inline_keyboard": [row]} if row else None


def _is_button_url(url: str | None) -> bool:
    """URL ボタンに載せてよい URL か。

    http(s)・ASCII・空白や制御文字なし・ホスト名の文字が正しいものだけ(Telegram が拒否すると
    sendMessage が 400 になり、再送でも同じボタンを付けるのでカードが届かなくなる)。
    """
    if url is None or len(url) > _URL_BUTTON_MAX or not url.isascii():
        return False
    if any(ch <= " " or ch == "\x7f" for ch in url):
        return False
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and _HOST_RE.fullmatch(host) is not None


# --- DB アクセス ---


def _load_items(conn: sqlite3.Connection, item_ids: list[int]) -> list[sqlite3.Row]:
    if not item_ids:
        return []
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"""
        SELECT items.id AS item_id, items.title, items.canonical_url, items.published_at,
               sources.name AS source_name,
               analyses.title_ja, analyses.summary, analyses.implication,
               analyses.recommendation_reason, analyses.degraded, analyses.extraction_failed
        FROM items
        JOIN sources ON sources.id = items.source_id
        JOIN analyses ON analyses.id = (
            SELECT id FROM analyses WHERE item_id = items.id
            ORDER BY analyzed_at DESC, id DESC LIMIT 1
        )
        WHERE items.id IN ({placeholders})
        """,
        item_ids,
    ).fetchall()
    by_id = {row["item_id"]: row for row in rows}
    missing = [i for i in item_ids if i not in by_id]
    if missing:
        raise DeliveryError(f"digest の item が items/analyses に無い: {missing}")
    return [by_id[i] for i in item_ids]  # 配信順を維持


def _load_ledger(conn: sqlite3.Connection, digest_id: int) -> tuple[set[int], bool]:
    # 今の世代の行だけを見る。--force の置換で印を付けた前の世代の行は、送り直しの判定に
    # 使わない(リプライの帰属と感想の投影のために残している。GH#190)
    rows = conn.execute(
        "SELECT item_id FROM digest_messages WHERE digest_id = ? AND superseded_at IS NULL",
        (digest_id,),
    ).fetchall()
    item_ids = {row["item_id"] for row in rows if row["item_id"] is not None}
    header_sent = any(row["item_id"] is None for row in rows)
    return item_ids, header_sent


def _record_message(
    conn: sqlite3.Connection,
    digest_id: int,
    item_id: int | None,
    message_id: int,
    logger: logging.Logger,
    run_id: str | None,
    sleep: Callable[[float], None],
) -> None:
    # 接続は autocommit(app/db.py)。1通ごとに即確定し、途中クラッシュでも
    # 台帳と送信実績がずれない。
    # 送信は済んでいるので、記帳に失敗しても送り直さず記帳だけをやり直す(GH#225。ディスクの
    # 一時的な異常など)。台帳に無い message は再走で未送信として送り直され、リプライの帰属も
    # 引けない。やり直しても記帳できなければ message_id をログに残して止める(runbook §15)
    error: sqlite3.Error | None = None
    for attempt in range(RECORD_RETRIES + 1):
        if attempt > 0:
            sleep(BACKOFF_S[attempt - 1])
        try:
            conn.execute(
                "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (?, ?, ?)",
                (digest_id, item_id, message_id),
            )
            return
        except sqlite3.OperationalError as e:  # ディスク満杯・I/O エラーなど。やり直す
            error = e
        except sqlite3.Error as e:  # 制約違反などはやり直しても通らない
            error = e
            break
    logger.error(
        "送信済みの message を台帳に記帳できない(この 1 通は台帳に無い。"
        "再走の前に runbook §15 で台帳に入れる)",
        extra={
            "run_id": run_id,
            "stage": "deliver",
            "item_id": item_id,  # null = ヘッダ
            "extra": {
                "digest_id": digest_id,
                "message_id": message_id,
                "error": f"{type(error).__name__}: {error}",
            },
        },
    )
    raise DeliveryError(
        f"送信済みの message_id={message_id} を台帳に記帳できない"
        f"(digest_id={digest_id}, item_id={item_id}): {type(error).__name__}"
    ) from error
