from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

ALL_COMMANDS = (
    "collect", "extract", "analyze", "digest", "deliver", "dig", "answer", "poll", "run",
    "doctor", "x-auth", "x-signals", "x-insights",
)


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ALL_COMMANDS:
        assert command in result.output


def test_doctor_accepts_documented_options(tmp_path, monkeypatch) -> None:
    # --purge/--cost が受理され、隔離した config_dir + 空 DB で完走する
    from app import logs

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない(GH#207)
    (tmp_path / "config.toml").write_text(
        f'[storage]\ndb_path = "{tmp_path}/intel.db"\n', encoding="utf-8"
    )
    result = runner.invoke(
        app, ["doctor", "--config-dir", str(tmp_path), "--purge", "--cost"]
    )
    assert result.exit_code == 0
    assert "purge 完了" in result.output
    assert "text_excerpt 0 件 NULL 化" in result.output  # doctor #11 の WARN と同じ単位(GH#250)
    assert "着地見込み" in result.output
    assert (tmp_path / "logs" / "morning.log").exists()  # purge の記録は隔離先に書かれる


def _answer_config(tmp_path, *, telegram: bool) -> None:
    lines = [f'[storage]\ndb_path = "{tmp_path}/intel.db"\n']
    if telegram:
        lines.append("[telegram]\nowner_user_id = 1\nowner_chat_id = 10\n")
        env = tmp_path / ".env"
        env.write_text("TELEGRAM_BOT_TOKEN=test-token\n", encoding="utf-8")
        env.chmod(0o600)  # group/other に開いた .env は読まない(設計 00 §4)
    (tmp_path / "config.toml").write_text("".join(lines), encoding="utf-8")


def test_answer_requires_telegram(tmp_path) -> None:
    _answer_config(tmp_path, telegram=False)
    result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1


def test_answer_exits_quietly_while_another_runs(tmp_path) -> None:
    # 同時に 2 つ走ると同じ質問に 2 回答えうる(GH#126)。後から来た方は DB にも触れない
    import fcntl

    _answer_config(tmp_path, telegram=True)
    with (tmp_path / "answer.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "別の answer が実行中" in result.output
    assert not (tmp_path / "intel.db").exists()


def test_dig_exits_quietly_while_another_digs(tmp_path, monkeypatch) -> None:
    # 朝バッチの dig 段が進めている間の intel dig は、同じ候補を 2 度掘らずに理由を出して終わる
    # (GH#196)
    import fcntl

    from app import logs

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    with (tmp_path / "intel.db.dig.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, ["dig", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "深掘りを進めているため、何もしなかった" in result.output


# 配信のテストの当日と前日(JST)。実際の日付に依らないよう today_jst を差し替える
_TODAY, _YESTERDAY = "2026-10-24", "2026-10-23"


class _RecordingApi:
    """送った本文を順に記録する Telegram の代役。"""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.texts.append(text)
        return 900 + len(self.texts)


def _undelivered_digests(
    tmp_path, monkeypatch, titles_by_date: dict[str, list[str]]
) -> tuple[_RecordingApi, dict[str, int]]:
    """日付ごとに記事を載せた未配信の digest がある DB を用意する(当日 = _TODAY)。
    送った本文の記録先と、日付ごとの digest の id を返す。"""
    import json

    import app.delivery as delivery
    from app import jst, logs
    from app.db import apply_migrations, connect

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    monkeypatch.setattr(jst, "today_jst", lambda: _TODAY)
    _answer_config(tmp_path, telegram=True)
    conn = connect(tmp_path / "intel.db")
    apply_migrations(conn, tmp_path / "intel.db")
    source_id = conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')").lastrowid
    digest_ids = {}
    for day, titles in titles_by_date.items():
        item_ids = []
        for title in titles:
            url = f"https://a/{day}/{len(item_ids)}"
            item_id = conn.execute(
                "INSERT INTO items (source_id, url, canonical_url, title, content_hash, status) "
                "VALUES (?, ?, ?, ?, 'sha256:x', 'digested')",
                (source_id, url, url, title),
            ).lastrowid
            conn.execute(
                "INSERT INTO analyses (item_id, summary, category, relevance_score, novelty, "
                "recommendation_reason, implication, engine, model, prompt_version, input_hash) "
                "VALUES (?, '要約', '課題直結', 80, 'new', '理由', '示唆', 'codex-cli', 'm', "
                "'analyze-v001', ?)",
                (item_id, f"sha256:{url}"),
            )
            item_ids.append(item_id)
        digest_ids[day] = conn.execute(
            "INSERT INTO digests (digest_date, markdown, item_ids, item_count) "
            "VALUES (?, '# md', ?, ?)",
            (day, json.dumps(item_ids), len(item_ids)),
        ).lastrowid
    conn.close()
    api = _RecordingApi()
    monkeypatch.setattr(delivery, "create_api", lambda env: api)
    return api, digest_ids


def _delivery_state(tmp_path, digest_id: int) -> tuple[str, int]:
    """digest の配信状態と、台帳に載った message の数。"""
    from app.db import connect

    conn = connect(tmp_path / "intel.db")
    status = conn.execute(
        "SELECT delivery_status FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()[0]
    count = conn.execute(
        "SELECT COUNT(*) FROM digest_messages WHERE digest_id = ?", (digest_id,)
    ).fetchone()[0]
    conn.close()
    return status, count


def test_deliver_date_sends_past_unsent_digest(tmp_path, monkeypatch) -> None:
    # 配信の失敗が翌日に持ち越されても、前日の未配信の digest を日付で指定して送れる(GH#247)。
    # 当日の digest には触れない(過去の分を自動で繰り越さない)
    yesterday, today = _YESTERDAY, _TODAY
    api, ids = _undelivered_digests(
        tmp_path, monkeypatch, {yesterday: ["前日の記事"], today: ["当日の記事"]}
    )

    result = runner.invoke(app, ["deliver", "--config-dir", str(tmp_path), "--date", yesterday])

    assert result.exit_code == 0, result.output
    assert "配信完了(2通)" in result.output
    assert len(api.texts) == 2
    assert yesterday in api.texts[0]  # ヘッダはその日の digest の日付のまま
    assert "前日の記事" in api.texts[1]
    assert _delivery_state(tmp_path, ids[yesterday]) == ("sent", 2)
    assert _delivery_state(tmp_path, ids[today]) == ("pending", 0)


def test_deliver_date_does_not_resend_delivered_messages(tmp_path, monkeypatch) -> None:
    # 途中まで届いた前日の分は、台帳にある分(ヘッダと 1 件目)を重ねて送らない。
    # 送り終えた後にもう一度打っても配信済みとして何も送らない(GH#247)
    from app.db import connect

    yesterday = _YESTERDAY
    api, ids = _undelivered_digests(
        tmp_path, monkeypatch, {yesterday: ["届いた記事", "残りの記事"]}
    )
    conn = connect(tmp_path / "intel.db")
    delivered_item = conn.execute(
        "SELECT id FROM items WHERE title = '届いた記事'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO digest_messages (digest_id, item_id, message_id) "
        "VALUES (?, NULL, 501), (?, ?, 502)",
        (ids[yesterday], ids[yesterday], delivered_item),
    )
    conn.execute(
        "UPDATE digests SET delivery_status = 'failed' WHERE id = ?", (ids[yesterday],)
    )
    conn.close()
    args = ["deliver", "--config-dir", str(tmp_path), "--date", yesterday]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert "配信完了(1通)" in first.output
    assert second.exit_code == 0, second.output
    assert "配信済み(skip)" in second.output
    assert len(api.texts) == 1
    assert "残りの記事" in api.texts[0]
    assert _delivery_state(tmp_path, ids[yesterday]) == ("sent", 3)


def test_deliver_without_date_sends_only_today(tmp_path, monkeypatch) -> None:
    # 日付を指定しない通常の配信は従来どおり当日分だけ。前日の未配信は拾わない(GH#247)
    yesterday, today = _YESTERDAY, _TODAY
    api, ids = _undelivered_digests(
        tmp_path, monkeypatch, {yesterday: ["前日の記事"], today: ["当日の記事"]}
    )

    result = runner.invoke(app, ["deliver", "--config-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert len(api.texts) == 2
    assert today in api.texts[0]
    assert "当日の記事" in api.texts[1]
    assert _delivery_state(tmp_path, ids[today]) == ("sent", 2)
    assert _delivery_state(tmp_path, ids[yesterday]) == ("pending", 0)


def test_deliver_date_accepts_unpadded_date(tmp_path, monkeypatch) -> None:
    # 0 埋めしない日付も DB の digest_date の形に揃えて引く
    api, ids = _undelivered_digests(tmp_path, monkeypatch, {"2026-01-05": ["記事"]})

    result = runner.invoke(app, ["deliver", "--config-dir", str(tmp_path), "--date", "2026-1-5"])

    assert result.exit_code == 0, result.output
    assert _delivery_state(tmp_path, ids["2026-01-05"]) == ("sent", 2)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026/01/05", "--date が不正"),
        ("2026-02-30", "--date が不正"),
        ("2026-01-06", "2026-01-06 の digest が存在しない"),
    ],
)
def test_deliver_date_rejects_bad_or_missing_date(tmp_path, monkeypatch, value, message) -> None:
    api, _ = _undelivered_digests(tmp_path, monkeypatch, {"2026-01-05": ["記事"]})

    result = runner.invoke(app, ["deliver", "--config-dir", str(tmp_path), "--date", value])

    assert result.exit_code == 1
    assert message in result.output
    assert api.texts == []


def _unsent_take(tmp_path, monkeypatch) -> Path:
    """返事が未送信の take が 1 件ある DB と vault を用意し、vault の置き場を返す。"""
    from app import logs
    from app.db import apply_migrations, connect

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    vault = tmp_path / "vault"
    (vault / "SignalForge" / "DeepDive").mkdir(parents=True)
    (vault / "SignalForge" / "DeepDive" / "note.md").write_text(
        "---\nstatus: take待ち\n---\n本文\n", encoding="utf-8"
    )
    with (tmp_path / "config.toml").open("a", encoding="utf-8") as f:
        f.write(f'[obsidian]\nvault_path = "{vault}"\n')
    conn = connect(tmp_path / "intel.db")
    apply_migrations(conn, tmp_path / "intel.db")
    conn.execute("INSERT INTO sources (type, name) VALUES ('rss', 'feed')")
    conn.execute(
        "INSERT INTO items (id, source_id, url, canonical_url, content_hash) "
        "VALUES (1, 2, 'https://a/1', 'https://a/1', 'sha256:x')"
    )
    conn.execute(
        "INSERT INTO dig_results (id, item_id, note_name, card_text, message_id) "
        "VALUES (1, 1, 'note', 'カード', 700)"
    )
    conn.execute(
        "INSERT INTO dig_questions (dig_result_id, question, question_message_id, status, kind, "
        "answer) VALUES (1, '結論はこう', 81, 'answered', 'take', '返事')"  # 返事は未送信
    )
    conn.close()
    return vault


def test_answer_records_takes_after_replies(tmp_path, monkeypatch) -> None:
    # 返事を送った後に take を記録する(GH#106)。送る時点では未記録で、同じ実行の終わりに記録済み
    import app.delivery as delivery
    from app.db import connect

    vault = _unsent_take(tmp_path, monkeypatch)
    recorded_at_send: list = []

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            check = connect(tmp_path / "intel.db")
            recorded_at_send.append(
                check.execute("SELECT take_recorded_at FROM dig_questions").fetchone()[0]
            )
            check.close()
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())

    result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert recorded_at_send == [None]  # 送った時点ではまだ記録していない
    assert "送信 1 通・take の記録 1 件" in result.output
    text = (vault / "SignalForge" / "DeepDive" / "note.md").read_text(encoding="utf-8")
    assert "結論はこう" in text


def test_answer_keeps_lock_while_stalled_vault_thread_may_write(tmp_path, monkeypatch) -> None:
    # vault が止まったら、止まったスレッドが後から追記を終えうる間(プロセスの終了まで)
    # ロックを握り、次の answer に同じ take を書かせない(GH#145)
    import fcntl
    import threading

    import app.delivery as delivery
    from app import dig_take, vault_guard

    _unsent_take(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(dig_take, "_append_to_note", lambda config, row: release.wait())
    real_guard = vault_guard.VaultGuard
    monkeypatch.setattr(vault_guard, "VaultGuard", lambda: real_guard(timeout_s=0.05))

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())
    try:
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "送信 1 通・take の記録 0 件" in result.output
        # このテストのプロセスが終わるまでロックは外れない(複製した fd は閉じない)
        with (tmp_path / "answer.lock").open("w") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()


def test_answer_keeps_lock_when_interrupted_while_vault_thread_lives(tmp_path, monkeypatch) -> None:
    # 待っている途中で中断(KeyboardInterrupt など)されても、ロックは外さない(GH#145 round 2)
    import fcntl
    import threading

    import app.delivery as delivery
    from app import dig_take, vault_guard

    _unsent_take(tmp_path, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(dig_take, "_append_to_note", lambda config, row: release.wait())

    class InterruptedGuard(vault_guard.VaultGuard):
        def call(self, fn):
            threading.Thread(target=fn, daemon=True).start()  # 書き込み直前で待つスレッド
            raise KeyboardInterrupt

    monkeypatch.setattr(vault_guard, "VaultGuard", InterruptedGuard)

    class Api:
        def send_message(self, chat_id, text, *, reply_to_message_id=None):
            return 82

    monkeypatch.setattr(delivery, "create_api", lambda env: Api())
    try:
        result = runner.invoke(app, ["answer", "--config-dir", str(tmp_path)])
        assert result.exit_code == 130  # 中断で終わる(typer は SIGINT と同じ 130 で抜ける)
        with (tmp_path / "answer.lock").open("w") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()


class _StopAtFactory(Exception):
    pass


@pytest.mark.parametrize(
    ("command", "stage"),
    [("analyze", "analyze"), ("digest", "trend"), ("dig", "dig"), ("answer", "answer")],
)
def test_commands_build_engines_through_fallback(tmp_path, monkeypatch, command, stage) -> None:
    # 主のエンジンを使う各コマンドは、段の名前で予備への切り替え(GH#156)を通して作る
    from app import logs
    from app.analysis import fallback

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    (tmp_path / "interest-profile.md").write_text(
        "---\nupdated: 2026-10-20\n---\n# 関心\n", encoding="utf-8"
    )
    # spy の中の assert は digest の except に吸われるので、記録して後で確かめる
    seen: list[tuple[str, dict]] = []

    def spy(conn, config, env, logger, *, stage, run_id=None, primary=None):
        seen.append((stage, dict(env)))
        raise _StopAtFactory

    monkeypatch.setattr(fallback, "fallback_engine_factory", spy)
    result = runner.invoke(app, [command, "--config-dir", str(tmp_path)])
    assert [name for name, _ in seen] == [stage]
    assert seen[0][1].get("TELEGRAM_BOT_TOKEN") == "test-token"  # .env の dict を渡す
    if command != "digest":  # digest はトレンドの失敗を握って本体を続ける(付加機能)
        assert isinstance(result.exception, _StopAtFactory)


@pytest.mark.parametrize(
    ("args", "shared", "returned", "passed", "output"),
    [
        (["analyze"], "analyze_stage", {"analyzed": 3, "degraded": 1}, {}, "analyzed=3 degraded=1"),
        (["digest"], "digest_stage", None, {"force": False}, "skip(生成済み)"),
        (["digest", "--force"], "digest_stage", 8, {"force": True}, "digest id=8"),
        (["dig"], "dig_stage", True, {}, "dig 完了"),
    ],
)
def test_commands_run_same_stage_as_morning_batch(
    tmp_path, monkeypatch, args, shared, returned, passed, output
) -> None:
    # 単独の CLI は、朝バッチ(run_pipeline)と同じ段の組み立てと実行の口を呼び、表示だけを
    # 自分で持つ(GH#206)。run_id と主の作り方は渡さない(主は config の `[llm].engine`)
    from app import logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    _answer_config(tmp_path, telegram=True)
    calls: list[tuple[dict, dict]] = []

    def spy(conn, config, env, logger, **kwargs):
        calls.append((dict(env), kwargs))
        return returned

    monkeypatch.setattr(run, shared, spy)
    result = runner.invoke(app, [*args, "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert [kwargs for _, kwargs in calls] == [passed]
    assert calls[0][0].get("TELEGRAM_BOT_TOKEN") == "test-token"  # .env の dict を渡す
    assert output in result.output


def test_run_notifies_fallback_after_pipeline(tmp_path, monkeypatch) -> None:
    # 朝バッチは、段が成功していても予備の利用(GH#156)を知らせる経路を通る
    from app import logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=True)
    calls: list[str] = []
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        run, "notify_llm_fallback", lambda conn, config, env, run_id, logger: calls.append(run_id)
    )
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0]


def test_run_failure_notice_gets_date_of_failed_delivery(tmp_path, monkeypatch) -> None:
    # deliver 段が送ろうとした digest の日付(run_pipeline の戻り値)を失敗通知へ渡す。
    # 通知の時点の日付ではなく、この日付で送り直し方を添える(GH#247)
    from app import delivery, logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=True)
    failures = run.RunFailures(["deliver"])
    failures.deliver_date = "2026-10-23"
    passed: list[str | None] = []
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: failures)
    monkeypatch.setattr(run, "notify_llm_fallback", lambda *args: None)
    monkeypatch.setattr(
        delivery,
        "notify_run_failure",
        lambda *args, digest_held, deliver_date: passed.append(deliver_date),
    )
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert passed == ["2026-10-23"]


@pytest.mark.parametrize("digest_held", [True, False])
def test_run_failure_notice_tells_whether_digest_was_held(
    tmp_path, monkeypatch, digest_held
) -> None:
    # digest 段が保留した事実(run_pipeline の戻り値)を失敗通知へ渡す(GH#191)
    from app import delivery, logs, run

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=True)
    failures = run.RunFailures(["analyze"])
    failures.digest_held = digest_held
    passed: list[bool] = []
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: failures)
    monkeypatch.setattr(run, "notify_llm_fallback", lambda *args: None)
    monkeypatch.setattr(
        delivery,
        "notify_run_failure",
        lambda *args, digest_held, **kwargs: passed.append(digest_held),
    )
    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert passed == [digest_held]


def _material_request(tmp_path):
    """材料不足のリプライ指定を 1 件積み、dig 段の選定と同じ候補(compare-and-set の写し)を返す。"""
    from app.config import Config
    from app.db import apply_migrations, connect
    from app.deepdive import _select_candidates

    db_path = tmp_path / "intel.db"
    conn = connect(db_path)
    apply_migrations(conn, db_path)
    conn.execute(
        "INSERT INTO sources (type, name, config) VALUES ('rss', 'feed-a', '{\"url\": \"https://a\"}')"
    )
    item_id = conn.execute(
        "INSERT INTO items (source_id, url, canonical_url, content_hash) "
        "VALUES (1, 'https://q/1', 'https://q/1', 'sha256:q')"
    ).lastrowid
    conn.execute(
        "INSERT INTO dig_queue (item_id, status, source, angle, requested_at) "
        "VALUES (?, 'queued', 'request', '観点', '2026-07-21T00:00:00Z')",
        (item_id,),
    )
    _, skipped = _select_candidates(conn, Config(config_dir=tmp_path), 2, "2026-07-22")
    assert [c.item_id for c in skipped] == [item_id]
    return conn, item_id, skipped


@pytest.mark.parametrize(
    ("case", "released"),
    [
        ("sent", True),
        ("already_notified", False),  # 今日は通知済み。次の朝に知らせてから外す
        ("send_failed", False),
        ("telegram_unset", False),
        ("respecified", False),  # 選定の後に本人が指定し直した。本人の操作を残す
    ],
)
def test_run_releases_material_request_only_after_failure_notice_is_sent(
    tmp_path, monkeypatch, case, released
) -> None:
    # 材料不足のリプライ指定は、失敗の通知をこの実行で送れた後にだけ外す(GH#337)。送れなかった
    # ときは指定のまま残し、次の朝の実行がもう一度知らせてから外す(知らせ漏れを作らない)
    from app import delivery, logs, run
    from app.delivery.state import set_state
    from app.delivery.surveys import STATE_RUN_FAILURE_NOTIFIED
    from app.delivery.telegram import DeliveryError
    from app.jst import today_jst
    from tests.test_poller import FakeApi

    class FailingApi(FakeApi):
        def send_message(self, chat_id, text, *, reply_markup=None):
            raise DeliveryError("送信失敗")

    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")
    _answer_config(tmp_path, telegram=case != "telegram_unset")
    conn, item_id, candidates = _material_request(tmp_path)
    if case == "already_notified":
        set_state(conn, STATE_RUN_FAILURE_NOTIFIED, today_jst())
    if case == "respecified":
        conn.execute(
            "UPDATE dig_queue SET angle = '新しい観点', requested_at = '2026-07-21T21:00:00Z' "
            "WHERE item_id = ?",
            (item_id,),
        )
    failures = run.RunFailures(["dig"])
    failures.material_requests = tuple(candidates)
    api = FailingApi() if case == "send_failed" else FakeApi()
    monkeypatch.setattr(run, "run_pipeline", lambda *args, **kwargs: failures)
    monkeypatch.setattr(run, "notify_llm_fallback", lambda *args: None)
    monkeypatch.setattr(delivery, "create_api", lambda env: api)

    result = runner.invoke(app, ["run", "--config-dir", str(tmp_path)])
    assert result.exit_code == 1  # 段の失敗のまま(外せたかで終了コードを変えない)
    assert len(api.sent) == (1 if case in ("sent", "respecified") else 0)
    requested_at = conn.execute(
        "SELECT requested_at FROM dig_queue WHERE item_id = ?", (item_id,)
    ).fetchone()["requested_at"]
    if released:
        assert requested_at is None
    else:
        assert requested_at is not None
    conn.close()


# config.toml を読む全コマンド(検査だけの doctor を除く)。DB・外部・設定の置き場に書くか、
# 既定の DB を読む(GH#338)
_CONFIG_REQUIRED = [
    ["collect"], ["extract"], ["analyze"], ["digest"], ["deliver"], ["dig"], ["answer"],
    ["poll"], ["run"], ["x-auth"], ["x-signals"], ["x-insights"],
    ["doctor", "--purge"], ["doctor", "--cost"],
]


@pytest.mark.parametrize("args", _CONFIG_REQUIRED, ids=" ".join)
def test_commands_stop_before_db_when_config_toml_is_missing(tmp_path, monkeypatch, args) -> None:
    # --config-dir の誤指定や設定の消失で、既定の設定のまま既定の DB を変更しない(GH#338)
    from app import logs
    from app.config import ConfigError

    home = tmp_path / "home"  # 既定の DB(~/.local/share/intel/intel.db)を本物の HOME から外す
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    conf = tmp_path / "conf"
    conf.mkdir()  # 置き場はあるが config.toml が無い
    result = runner.invoke(app, [*args, "--config-dir", str(conf)])
    assert result.exit_code == 1
    assert isinstance(result.exception, ConfigError)
    assert str(conf / "config.toml") in str(result.exception)  # 探した path を出す
    assert not home.exists()  # 既定の DB も、DB の隣のロックも作らない
    assert not (tmp_path / "logs").exists()


def test_main_reports_config_error_in_one_line(tmp_path, monkeypatch, capsys) -> None:
    # 入口(intel = app.cli:main)は設定の不備を traceback でなく 1 行で出して止める(GH#338)
    import sys

    from app import logs
    from app.cli import main

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # DEFAULT_LOG_DIR は import の時点の HOME で決まるので、HOME の差し替えでは外れない
    monkeypatch.setattr(logs, "DEFAULT_LOG_DIR", tmp_path / "logs")  # 本物のログに書かない
    monkeypatch.setattr(sys, "argv", ["intel", "doctor", "--purge", "--config-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"設定エラー: {tmp_path / 'config.toml'} が存在しない")
    assert err.count("\n") == 1  # traceback を付けない
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "logs").exists()
