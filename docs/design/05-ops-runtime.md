# 設計 05: 運用・ランタイム(ops-runtime)

ステータス: Draft v1(2026-07-05)/ 正本: 企画は [../PROPOSAL.md](../PROPOSAL.md)。本書は運用設計の正本。

対象範囲: launchd 構成、`intel doctor`、ログ、失敗通知、バックアップ、secrets、月次コストレポート、リトライ/タイムアウト規約、開発フロー。パイプライン内部(collect/analyze/digest の処理内容)は 01〜04 の各設計書に委ねる。

## 1. launchd 構成(2プロセス)

plist は `ops/launchd/` に置き、`~/Library/LaunchAgents/` に symlink せず**コピーして `launchctl bootstrap gui/$UID`**(symlink は macOS で無視されることがあるため)。LaunchDaemon ではなく **LaunchAgent**(GUI セッションの Keychain・TCC 許可を使うため)。

### 1.1 朝バッチ `com.intel.morning`

- `ProgramArguments`: `[uv, run, intel, run]`(作業ディレクトリ `WorkingDirectory` = リポジトリ)。`intel run` = **collect→extract→analyze→digest→export→deliver→purge** の直列実行(ステージ順序の正本は設計 00 §2。deliver 失敗でも export は完了済みで `delivery_status='failed'` を記録)。
- `StartCalendarInterval`: 06:30 JST(配信 7:00 の30分前。LLM バッチと X ページングの余裕をみる)。
- **再起動方針: 自動再実行しない**。`intel run` は冪等(sources のカーソル管理+items の content_hash 重複排除)なので失敗時は手動 `intel run` で安全に再走できる。自動リトライを launchd に持たせない理由: 二重課金・二重配信の検知より、watchdog 通知→手動再走の方が単純で事故が少ない。
- スリープ対策: `sudo pmset repeat wakeorpoweron MTWRFSU 06:25:00`。StartCalendarInterval はスリープ中スキップされるため wake を先に確保する。
- `EnvironmentVariables` は `PATH` と `HOME` のみ。**`ANTHROPIC_API_KEY` を絶対に入れない**(claude -p が API 課金に倒れるのを構成で防ぐ)。

### 1.2 受信デーモン `com.intel.poller`

- `ProgramArguments`: `[uv, run, intel, poll]`(Telegram long polling 常駐。ボタン反応・手動 URL・夜アンケートを受信し signals / items に記録)。
- **再起動方針: `KeepAlive: true` + `ThrottleInterval: 30`**。クラッシュしても callback query の取りこぼしを最小化するため常時復帰。連続クラッシュ(30秒間に再起動が繰り返される状態)は launchd が抑制し、doctor と watchdog で検知する。
- 夜アンケート送信(**21:30 JST 仮**、config で変更可。設計 04 §4 と同一方式・同一時刻)と watchdog(7:15 JST に digests の当日行を確認、無ければ失敗通知)はポーラー内のタイマーで実行する。理由: plist を2枚に抑え、Telegram 送信経路を1プロセスに集約するため(別 launchd ジョブによる `--survey` 起動は採らない)。送信ロジック自体は `delivery/telegram.py` の関数(実装の置き場は 04 が正本)。

### 1.3 事前セットアップ(doctor で検査)

実行ユーザーの `uv` バイナリへ **Full Disk Access** 付与(launchd 背景ジョブの Obsidian Vault 書き込みが TCC で止まるため)。付与確認は doctor の書き込みテストで行う。

## 2. `intel doctor` チェック項目

終了コード: 全 OK=0 / WARN のみ=0(表示のみ)/ FAIL あり=1。

| # | 項目 | 判定 |
|---|------|------|
| 1 | config.toml 存在・スキーマ検証(Pydantic) | FAIL |
| 2 | `.env` のパーミッション(600 以外は FAIL)・必須キー存在 | FAIL |
| 3 | SQLite 接続+マイグレーション適用状態(未適用番号の検出) | FAIL |
| 4 | Telegram: `getMe` 成功+allowlist(user_id/chat_id)設定済み | FAIL |
| 5 | X API: 認証確認(無料エンドポイントで疎通のみ。課金読み取りはしない)。**X 無効時(config `x.enabled = false`、または sources の type='x' 全行が enabled=0)は SKIP 表示**(「RSS+手動URLのみ」縮退運用を恒久 FAIL にしない。設定はあるが無効化されている場合は WARN 1行で明示) | FAIL(X 無効時は SKIP/WARN) |
| 6 | LLM: 設定エンジン(MVP は1本。設計 00 §5)で固定プロンプト1件の `complete_structured` 成功+推論専用モードの**動的検査**(ツール呼び出し・ファイル読み取りを試みるプロンプトを流し、拒否されることを確認する。設定値の静的確認だけでは MCP・設定側 allowlist の残存を検出できない) | FAIL |
| 7 | Obsidian Vault パスへのテスト書き込み・削除(TCC/FDA 検査を兼ねる) | FAIL |
| 8 | launchd 2ジョブのロード状態(`launchctl print`) | WARN |
| 9 | 当月コスト見込み(analyses.estimated_cost 合算+X 読取件数×単価)と $30 比 | 超過ペースで WARN |
| 10 | バックアップ最新世代の日付(48h 超で WARN)+アーカイブのパーミッション(600 以外は WARN) | WARN |
| 11 | documents の TTL 超過残存件数+X 由来 `items.raw_payload` の TTL 超過残存件数(削除経路の故障検知。01 §6) | WARN |
| 12 | 環境に `ANTHROPIC_API_KEY` が存在したら WARN(無自覚 API 課金防止) | WARN |

## 3. ログ設計

- 出力先: `~/.local/state/intel/logs/`。`morning.log` / `poller.log`(launchd の stdout/stderr もここへ向ける)。
- 形式: **JSON Lines**(ts, level, run_id, stage, source_id, item_id, msg, extra)。理由: KPI 週次レビューや障害調査を `jq` の一発クエリで済ませるため。
- `run_id` は朝バッチ1回ごとの ULID。digests・analyses にも記録し、ログ⇔DB を突合可能にする。
- ローテーション: Python `logging.handlers.TimedRotatingFileHandler`(日次・14世代)。newsyslog 等の外部設定は持たない(依存最小)。
- レベル方針: 正常系は stage 境界の INFO 1行(件数・所要・推定コスト)。外部本文・プロンプト全文は**ログに書かない**(untrusted data の拡散と肥大化を防ぐ)。デバッグ時のみ `--verbose`。

## 4. 失敗時の Telegram 通知

- 通知チャネルは配信と同じ Bot・同じ chat(owner のみ)。通知種別:
  1. **朝バッチ失敗**: `intel run` が非0終了 → 末尾の例外要約1行+「`intel run` で再走」の定型文。
  2. **watchdog**: 7:15 に当日 digest 無し → 「今朝のダイジェスト未生成」(1 との重複は許容。取りこぼしゼロを優先)。
  3. **degraded 配信**: LLM 全滅でタイトルのみ配信した場合、ヘッダに明記(別通知は出さない)。
- レート制限: 同一種別は1日1通まで(通知疲れで watchdog が無視される事態を防ぐ)。
- 通知自体の失敗(Telegram 障害)はログ記録のみで諦める。フォールバック通知経路は作らない(拡張点)。

## 5. バックアップ

- 対象: SQLite 本体、`~/.config/intel/`(config.toml・.env・関心プロファイル)、`prompts/`(git 管理だが世代の一貫性のため同梱)。
- 方式: 朝バッチ末尾で `sqlite3 ... ".backup"`(オンラインバックアップ API。ファイルコピーは WAL 破損リスクがあるため不可)→ 設定類と併せ tar.gz、`~/Backups/intel/` に **日次14世代**。
- **secrets 拡散防止**: tar.gz は作成時に **umask/chmod 600 を強制**する(.env の X API キー・bot token・Anthropic キーが同梱されるため、.env 本体の 600 と同水準を複製側にも要求)。`~/Backups/intel/` は **クラウド同期(iCloud 等)の対象外に置く**(Time Machine 込みの扱いはユーザー判断だが、同期経由の secrets 拡散を既定で作らない)。パーミッションは doctor #10 が検査。
- 復旧手順: tar 展開→ `.backup` ファイルを DB パスへ配置→ `intel doctor`。Obsidian Vault はバックアップ対象外(SQLite が正本で、export/obsidian.py で再生成可能)。
- リモート退避(Time Machine 外のクラウド等)は拡張点に留める。

## 6. secrets 保管

- 置き場: `~/.config/intel/.env`(chmod 600、リポジトリ外)。キー: X API・Telegram bot token・(fallback 用)Anthropic API キー。Keychain 対応は拡張点(security CLI 呼び出しの抽象は config.py に閉じる)。
- **Anthropic キーは .env にのみ置き、シェル環境・launchd 環境に export しない**。AnthropicApiEngine だけが .env から直接読む。
- ローテーション手順(runbook): X = ポータルで再発行→.env 差し替え→doctor。Telegram = BotFather revoke→再発行→.env 差し替え→doctor(allowlist は chat_id 不変なので変更不要)。

## 7. 月次コストレポート

- 毎月1日の朝バッチ末尾で前月分を集計し、ダイジェストとは別メッセージで Telegram 配信: X 読取件数×実測単価 / LLM 推定コスト(analyses.estimated_cost 合算)とサブスク・クォータ消費率メモ / 合計と $30 比・超過時の推奨日次上限(逆算値)。
- 集計クエリは `intel doctor --cost` でいつでも単発実行可(判定材料の随時確認用)。X の課金メーター実測値との突合は月次の手動作業とする(API で取れる保証がないため自動化しない)。

## 8. リトライ / タイムアウト規約(全コンポーネント共通)

**本表がリトライ/タイムアウトの単一正本**(02 のエラー分類ほか各設計書は本表を参照する。数値の重複定義を持たない)。

| 経路 | timeout | リトライ | 備考 |
|---|---|---|---|
| HTTP 取得(RSS・本文・X) | connect 10s / read 30s | 2回、指数バックオフ(1s→4s)+ jitter | 429 は Retry-After 尊重。恒久 4xx はリトライしない |
| LLM `complete_structured` | 120s/呼 | 同一エンジン1回(失敗チャンクのみ再実行) | スキーマ検証失敗も同じ扱い。なお失敗は degraded 直行(エンジンフォールバックは拡張点。設計 00 §5) |
| Telegram 送信 | 30s | 3回、429 は retry_after 秒待ち | 全滅時は §4 の失敗系へ |
| Telegram long polling | long poll 50s | 即時再接続(連続失敗は30s間隔) | プロセスは KeepAlive が面倒を見る |

- **SSRF ガード(外部 URL を fetch する全経路の共通 HTTP クライアント規約)**: 本文抽出・短縮 URL 展開・RSS 取得を含むすべての外向き HTTP は、**DNS 解決後の接続先 IP が private / loopback / link-local 帯なら接続を拒否**し、**リダイレクト追跡の各ホップで再検査**する(悪意ある RSS entry・短縮 URL のリダイレクト先から、常時稼働機の LAN 内・localhost へのプローブを遮断)。
- 原則: **リトライはステージ内で完結**させ、ステージをまたぐ再実行は冪等な `intel run` 全体の手動再走に一本化する(部分再開の状態管理を持たない)。
- 個別ソースの失敗はそのソースをスキップして続行し、sources の成否時刻・consecutive_failures に記録(1本の死んだ RSS で朝が全滅しないため)。

## 9. 開発フロー

- **GitHub Issue → 小さい PR → merge** の反復。設計書 01〜05 の単位よりさらに細かく、レビュー可能な粒度(目安: 1 PR = 1 CLI サブコマンド or 1 コネクタ)で切る。
- **相互レビュー: Claude が実装し、Codex がレビューする**(author ≠ reviewer)。commit には `Co-Authored-By: Claude ...` トレーラを必ず付け、レビュー routing の判定材料にする。
- レビュー結果は PR 上に 🔴 must / 🟡 should / ⚪ nit の3段階で残し、**must ゼロを merge 条件**とする。should/nit の対応は author 判断。
- CI は最小: `uv run pytest` + `ruff check` を PR ごとにローカル実行(GitHub Actions 化は拡張点。個人リポジトリで秘匿情報を送らない判断を先に行う)。
- マイグレーション(番号付き .sql)を含む PR は、バックアップ取得→適用→doctor 通過をレビューチェック項目に含める。

## 10. 拡張点(実装しない・場所だけ確保)

- 通知のフォールバック経路(メール等)/ Keychain 保管 / バックアップのリモート退避 / KPI 集計の自動化(フェーズ2)/ GitHub Actions CI。
