# 設計 05: 運用・ランタイム(ops-runtime)

ステータス: Draft v1(2026-07-05)/ 正本: 企画は [../PROPOSAL.md](../PROPOSAL.md)。本書は運用設計の正本。

対象範囲: launchd 構成、`intel doctor`、ログ、失敗通知、バックアップ、secrets、月次コストレポート、リトライ/タイムアウト規約、開発フロー。パイプライン内部(collect/analyze/digest の処理内容)は 01〜04 の各設計書に委ねる。

## 1. launchd 構成(2プロセス)

plist は `ops/launchd/` に置き、`~/Library/LaunchAgents/` に symlink せず**コピーして `launchctl bootstrap gui/$UID`**(symlink は macOS で無視されることがあるため)。LaunchDaemon ではなく **LaunchAgent**(GUI セッションの Keychain・TCC 許可を使うため)。

### 1.1 朝バッチ `com.intel.morning`

- `ProgramArguments`: `[uv, run, intel, run]`(作業ディレクトリ `WorkingDirectory` = リポジトリ)。`intel run` = **collect→x_signals→extract→analyze→digest→export→deliver→dig→purge→backup** の直列実行(ステージ順序の正本は設計 00 §2。deliver 失敗でも export は完了済みで `delivery_status='failed'` を記録)。dig 段(GH#105)は 1 件あたり分オーダーの LLM 生成を含むが、deliver の後に走るので watchdog(当日のダイジェストの生成・配信と X ソースの連続失敗だけを見る)の判定には影響しない。dig の所要時間で後ろにずれるのは purge と backup の完了時刻と、朝バッチが poller と DB を共有する時間の長さ(上限 `[dig].daily_cap` を増やすときはこちらを見直す)。
- `StartCalendarInterval`: 06:30 JST(配信 7:00 の30分前。LLM バッチと X ページングの余裕をみる)。
- **再起動方針: 自動再実行しない**。`intel run` は冪等(sources のカーソル管理+items の content_hash 重複排除)なので失敗時は手動 `intel run` で安全に再走できる。自動リトライを launchd に持たせない理由: 二重課金・二重配信の検知より、watchdog 通知→手動再走の方が単純で事故が少ない。
- スリープ対策: `sudo pmset repeat wakeorpoweron MTWRFSU 06:25:00`。StartCalendarInterval はスリープ中スキップされるため wake を先に確保する。
- `EnvironmentVariables` は `PATH` と `HOME` のみ。**`ANTHROPIC_API_KEY` を絶対に入れない**(claude -p が API 課金に倒れるのを構成で防ぐ)。

### 1.2 受信デーモン `com.intel.poller`

- `ProgramArguments`: `[uv, run, intel, poll]`(Telegram long polling 常駐。ボタン反応・手動 URL・夜アンケートを受信し signals / items に記録)。
- **再起動方針: `KeepAlive: true` + `ThrottleInterval: 30`**。クラッシュしても callback query の取りこぼしを最小化するため常時復帰。連続クラッシュ(30秒間に再起動が繰り返される状態)は launchd が抑制する。連続クラッシュを自動で知らせる仕組みは無い(doctor #8 はジョブがロードされているかだけを見る。watchdog はこのプロセスの内蔵タイマーなので、落ちている間は動かない)。`poller-launchd.log` と `poller.log` で確かめる。
- 夜アンケート送信(**21:30 JST 仮**、config で変更可。設計 04 §4 と同一方式・同一時刻)と watchdog(7:15 JST に当日のダイジェストが未生成・未配信なら通知。X ソースの連続失敗も同じ 1 通で知らせる。§4)、週 1 回のソースの健康診断(日曜の夜アンケートの時刻。§4・GH#150)はポーラー内のタイマーで実行する。`telegram.watchdog_time` は**配信時刻+15分程度に設定する前提**(それ以前だと朝バッチ未完の時点で確認が走り誤通知になる)。理由: plist を2枚に抑え、Telegram 送信経路を1プロセスに集約するため(別 launchd ジョブによる `--survey` 起動は採らない)。送信ロジック自体は `delivery/telegram.py` の関数(実装の置き場は 04 が正本)。
- **深掘りカードへの質問への返事(GH#126)**: カード(とその返事)へのリプライを受けたポーラーは、質問を台帳 `dig_questions` に積み、`intel answer` を**別プロセス**(`python -m app.cli answer`)で起動する。LLM の待ち(数十秒〜数分)で受信と内蔵タイマーを止めないため。子は別セッションで動かし(ポーラーの再起動で作りかけの返事を道連れにしない)、子の側の flock で同時に 1 つに絞る。ポーラーは各周期の見回りで、答えていない質問が残っていれば起動し直し、送れていない返事・記録できていない take(GH#106)だけなら 10 分おきに起動する。子の詳細ログは `answer.log`。子が vault(解説・対訳ノートの読み取り、take の追記)に触れるときは 1 回ごとに時間の上限(10 秒)を付け、超えたらその実行では vault に触れずに先へ進む(ノートなしで返事を作り、take は次の実行に回す)。macOS のプライバシーの確認待ちで `open()` が止まると、子が flock を握ったまま返事の処理全体が止まるため(GH#145)。止まったスレッドは中断できず、後から追記を終えうるので、子は flock をプロセスの終了まで握る(正常の終わりでも中断でも、次の子が同じ take を書かない)。このため poller.plist の PATH にも codex の dir を入れる(#72 と同じ理由)。1 日の上限は `[dig].answer_daily_cap`(既定 20。0 = 返事をせず記録だけ)。

### 1.3 事前セットアップ(doctor で検査)

実行ユーザーの `uv` バイナリへ **Full Disk Access** 付与(launchd 背景ジョブの Obsidian Vault 書き込みが TCC で止まるため)。付与確認は doctor の書き込みテストで行う。

## 2. `intel doctor` チェック項目

終了コード: 全 OK=0 / WARN のみ=0(表示のみ)/ FAIL あり=1。

| # | 項目 | 判定 |
|---|------|------|
| 1 | config.toml 存在・スキーマ検証(Pydantic) | FAIL |
| 2 | `.env` のパーミッション(600 以外は FAIL)・必須キー存在(Claude API を使う設定なら `INTEL_ANTHROPIC_API_KEY` も: 深掘りのレビュー `[dig].review` を使い深掘りが有効なとき(GH#153)と、予備 `[llm_fallback].engine = anthropic-api` のとき(GH#156)。無くても本体は動くが、レビュー・予備が毎回失敗するため) | FAIL |
| 3 | SQLite 接続+マイグレーション適用状態(未適用番号の検出) | FAIL |
| 4 | Telegram: `getMe` 成功+allowlist(user_id/chat_id)設定済み | FAIL |
| 5 | X API: 認証確認 = `users/me` の実疎通(読取課金なし。GH#63)。成功 PASS(@username 表示)/ 401・403 = キー・権限の恒久異常で FAIL / 429・5xx・transport = 一時異常で WARN(恒久 FAIL にしない)/ 他の非 200・username 欠損 = 想定外の恒久異常で FAIL(WARN に埋めない)。**X 無効時(config `x.enabled = false`、または sources の type='x' 全行が enabled=0)は SKIP 表示**(「RSS+手動URLのみ」縮退運用を恒久 FAIL にしない。設定はあるが無効化されている場合は WARN 1行で明示)。X 有効時は **mode 整合検査**も行う: `[x_search].slots` ⟷ mode=search 行の相互欠落・同一 mode の複数 enabled 行(二重課金)・mode 不正行を WARN(設計 02 §2。mode WARN 時は疎通検査に進まない) | FAIL(X 無効時は SKIP/WARN) |
| 6 | LLM: 設定エンジン(MVP は1本。設計 00 §5)で固定プロンプト1件の `complete_structured` 成功+推論専用モードの**動的検査(隔離 canary 3プローブ)**。read-only sandbox はファイル読取・読取系コマンドの実行自体は許す(Issue #3 実測)ため「拒否確認」は構造的に不成立。代わりに**隔離が効いているか**を客観確認する: (1) HOME 隔離 = 実 HOME 直下の canary を `~` 相対で読ませ内容が漏れないこと (2) ネットワーク遮断 = ローカル canary サーバの URL を fetch させ到達が観測されないこと(自己申告に依存しない) (3) CODEX_HOME 隔離(codex-cli のみ)= auth.json と同居させた canary が漏れないこと(credential 流出経路の実測。auth 実内容は載せず canary で代替)。いずれか漏れ・到達で FAIL。**(3) は当初 read-only sandbox が読取を許すため真陽性 FAIL だった(Issue #54)→ engine 側を permission profile の deny-read に変更し塞いだ(設計 00 §5)。プローブ自体は不変** | FAIL |
| 7 | Obsidian Vault パスへのテスト書き込み・削除(TCC/FDA 検査を兼ねる) | FAIL |
| 8 | launchd 2ジョブのロード状態(`launchctl print`) | WARN |
| 9 | 当月コストの実績と着地見込み(LLM = `analyses.estimated_cost_usd` と深掘りのレビューの `dig_results.review_cost_usd`(GH#153)と予備の `llm_fallback_calls.cost_usd`(GH#156)の合算 + X の読み取り費用 = `collect_runs` と `x_signal_runs` の `estimated_cost_usd` の合算)と `[budget].monthly_cap_usd` の比 | 超過ペースで WARN |
| 10 | バックアップ最新世代の日付(48h 超で WARN)+アーカイブのパーミッション(600 以外は WARN) | WARN |
| 11 | documents の TTL 超過残存件数+**全ソース**の `items.raw_payload` の TTL 超過残存件数(ソース別 TTL — X: `x_cache_ttl_days` / RSS・manual: `cache_ttl_days` — で判定。削除経路の故障検知。01 §6) | WARN |
| 12 | 環境に `ANTHROPIC_API_KEY` が存在したら WARN(無自覚 API 課金防止) | WARN |
| 13 | X OAuth2 token(S1 = GH#58): token 有効性を `/2/users/me`(読取課金なし)で確認。**token 未取得は SKIP**(S1 取込を使うまで認可不要。恒久 FAIL にしない)。token ファイル破損・client_id 未設定(refresh 不能)・users/me 失敗は FAIL。refresh はローテーションを伴うため検査では強制しない(失効時のみセッションが refresh) | FAIL(未認可時は SKIP) |
| 14 | ソース死活: enabled なソースの `consecutive_failures` が X ≥ 3 / RSS ≥ 7(死亡疑い)なら WARN(設計 02 §2・§3。`x.enabled=false` の間の X は見ない)。自動 disable はしない | WARN |

## 3. ログ設計

- 出力先: `~/.local/state/intel/logs/`。`morning.log` / `poller.log`(launchd の stdout/stderr もここへ向ける)/ `answer.log`(深掘りカードへの質問に返事する `intel answer`。GH#126。poller とはプロセスが別なので、日次ローテーションが競合しないようファイルを分ける)。
- 形式: **JSON Lines**(ts, level, run_id, stage, source_id, item_id, msg, extra)。理由: KPI 週次レビューや障害調査を `jq` の一発クエリで済ませるため。
- `run_id` は朝バッチ1回ごとの ULID。digests・analyses にも記録し、ログ⇔DB を突合可能にする。
- ローテーション: Python `logging.handlers.TimedRotatingFileHandler`(日次・14世代)。newsyslog 等の外部設定は持たない(依存最小)。
- レベル方針: 正常系は stage 境界の INFO 1行(件数・所要・推定コスト)。外部本文・プロンプト全文は**ログに書かない**(untrusted data の拡散と肥大化を防ぐ)。詳細ログの切り替え(`setup_logging` の `verbose` 引数)は CLI からはまだ渡せない(`--verbose` オプションは未実装)。

## 4. 失敗時の Telegram 通知

- 通知チャネルは配信と同じ Bot・同じ chat(owner のみ)。通知種別:
  1. **朝バッチ失敗**: `intel run` で失敗した段がある → 失敗した段の一覧と run_id +「`intel run` で再走」の定型文(例外の詳細は `morning.log` を run_id で引く)。
  2. **watchdog**: 7:15 に当日 digest 無し → 「今朝のダイジェストが未生成」、digest はあるが配信されていない(`delivery_status` ≠ `sent`)→「未配信」(1 との重複は許容。取りこぼしゼロを優先)。X ソースの連続失敗(consecutive_failures ≥ 3。設計 02 §2)も同じ 1 通に行を足して知らせる(GH#124)。
  3. **degraded 配信**: LLM 全滅でタイトルのみ配信した場合、ヘッダに明記(別通知は出さない)。
  4. **ソースの健康診断(週 1 回。GH#150)**: 毎週日曜の夜アンケートの時刻に、連続で失敗しているソース・移転したソース・新しい記事が入らないソース・👍・🔖 の率が低いソースを 1 通にまとめて送る(設計 02 §3)。問題の無い週も送る(週次レビューの棚卸しの合図)。止める・URL を直す判断は人が行う(手順は runbook §12)。日曜の夜に poller が止まっていたら、その週は送らない(夜アンケートと同じく、日付を跨いだ分は送らない)。送れてから「送信済み」を記録する
  5. **予備への切り替え(GH#156)**: 朝バッチで、Codex が失敗した呼び出しを Claude API(`[llm_fallback]`)でやり直したら、段ごとの回数(うち予備も失敗した呼び出しの回数。段の中のやり直しで答えられた分も含むので、最終的な劣化・失敗は 1〜3 の通知とダイジェストで見る)・今回と今月の額を 1 通にまとめて送る(段は成功していても送る。Codex が止まっている合図)。朝バッチ 1 回ごとに 1 通で、再走でも使えばその分をもう 1 通送る。送れなければログだけ(終了コードは変えない)。何をするかは runbook §14
- レート制限: 同一種別は1日1通まで(通知疲れで watchdog が無視される事態を防ぐ。5 は朝バッチ 1 回ごと)。
- 通知自体の失敗(Telegram 障害)はログに残す。どちらの通知も送れてから「通知済み」を記録するので、問題が続いていれば次の機会に送り直す: watchdog は poller の次の周期で問題を判定し直して送る。朝バッチ失敗通知は次の `intel run`(再走)でも段が失敗したときに、その実行の失敗を送る(再走が成功すれば、前回の届かなかった通知は送らない)。フォールバック通知経路は作らない(拡張点)。

## 5. バックアップ

- 対象: SQLite 本体、`~/.config/intel/` の全体(config.toml・.env・関心プロファイル、X の OAuth 2.0 を使うなら `x-oauth2-token.json`)、`prompts/`(git 管理だが世代の一貫性のため同梱)。
- 方式: 朝バッチ末尾で `sqlite3 ... ".backup"`(オンラインバックアップ API。ファイルコピーは WAL 破損リスクがあるため不可)→ 設定類と併せ tar.gz、`~/Backups/intel/` に **日次14世代**。
- **secrets 拡散防止**: tar.gz は作成時に **umask/chmod 600 を強制**する(.env の X API キー・bot token・Anthropic キーと X の OAuth 2.0 token が同梱されるため、.env 本体の 600 と同水準を複製側にも要求)。`~/Backups/intel/` は **クラウド同期(iCloud 等)の対象外に置く**(Time Machine 込みの扱いはユーザー判断だが、同期経由の secrets 拡散を既定で作らない)。パーミッションは doctor #10 が検査。
- 復旧手順: tar 展開→ `.backup` ファイルを DB パスへ配置→ `intel doctor`。Obsidian Vault はバックアップ対象外。Daily / Items ノートの生成部分は SQLite のデータから書き出せる(ただし通常の `intel run` の export が書くのは当日分と、まだ反映していない感想のある分だけで、過去分を一括では書き直さない)が、DeepDive ノート(公開した後は本文を DB に残さない。設計 01 §7)と、ノートに足した手書きのメモ・take は vault にしか無いので、vault は別に保全する。
- リモート退避(Time Machine 外のクラウド等)は拡張点に留める。

## 6. secrets 保管

- 置き場: `~/.config/intel/.env`(chmod 600、リポジトリ外)。キー: X API・Telegram bot token・Anthropic API キー(`INTEL_ANTHROPIC_API_KEY`。深掘りノートのレビュー用。GH#153)・(confidential client のみ・任意)`X_OAUTH2_CLIENT_SECRET`。Keychain 対応は拡張点(security CLI 呼び出しの抽象は config.py に閉じる)。
- X OAuth2 token(S1): `~/.config/intel/x-oauth2-token.json`(chmod 600)。`intel x-auth` が生成し、refresh のローテーションで自動更新される(手編集しない)。無効化は X 側のアプリ連携解除 + ファイル削除。
- **Anthropic キーは .env にのみ置き、シェル環境・launchd 環境に export しない**。AnthropicApiEngine だけが .env から直接読む。キー名を `ANTHROPIC_API_KEY` にしないのは、環境に載ったときに claude -p が無自覚に API 課金へ回るのを防ぐため(doctor #12 が環境の `ANTHROPIC_API_KEY` を WARN にする)。
- ローテーション手順(runbook): X = ポータルで再発行→.env 差し替え→doctor。Telegram = BotFather revoke→再発行→.env 差し替え→doctor(allowlist は chat_id 不変なので変更不要)。

## 7. 月次コストレポート

> **現状(未実装。plan #25)**: 月次レポートの Telegram 配信はまだ無い。今あるのは**当月**の実績と着地見込みの集計だけで、doctor #9 と `intel doctor --cost` が出す(`app/doctor.py` の `month_cost_summary`)。以下は実装するときの設計。

- 毎月1日の朝バッチ末尾で前月分を集計し、ダイジェストとは別メッセージで Telegram 配信: X 読取件数×実測単価 / LLM 推定コスト(`analyses.estimated_cost_usd` の合算)とサブスク・クォータ消費率メモ / 合計と $30 比・超過時の推奨日次上限(逆算値)。
- 当月の集計は `intel doctor --cost` でいつでも単発実行できる(判定材料の随時確認用。前月分の集計は月次レポートとともに実装する)。X の課金メーター実測値との突合は月次の手動作業とする(API で取れる保証がないため自動化しない)。

## 8. リトライ / タイムアウト規約(全コンポーネント共通)

**本表がリトライ/タイムアウトの単一正本**(02 のエラー分類ほか各設計書は本表を参照する。数値の重複定義を持たない)。

| 経路 | timeout | リトライ | 備考 |
|---|---|---|---|
| HTTP 取得(RSS・本文・X) | connect 10s / read 30s | 2回、指数バックオフ(1s→4s)+ jitter | 429 は Retry-After 尊重。恒久 4xx はリトライしない |
| LLM `complete_structured`(analyze・トレンド) | 120s/呼 | 同一エンジン1回(analyze は失敗チャンクのみ再実行) | スキーマ検証失敗も同じ扱い。analyze はなお失敗なら degraded 直行(エンジンフォールバックは拡張点。設計 00 §5)。トレンドは失敗したら節を省くだけで digest を止めない(設計 03 §3.5) |
| LLM `complete_structured`(深掘りの調査段・対訳・質問への返事) | `[dig].timeout_s`(既定 600s)/呼 | 同一エンジン1回 | 調査段はアイテム単位で隔離し、失敗は段の失敗として朝バッチ失敗通知に載せる。返事は作れなければ失敗の知らせをリプライで送る(GH#126) |
| 予備への切り替え(`[llm_fallback]`。GH#156) | 呼び出し元と同じ | 主が失敗した呼び出しを予備で 1 回(その外側の同一エンジン 1 回のやり直しは各経路のまま)。主が続けて 2 回失敗したら(起動できないときは 1 回で)そのプロセスの残りは主を飛ばす | 予備も失敗・予備を作れない・月の上限なら、主の失敗と同じ種類のエラーにして各経路の扱い(劣化・段の失敗)に戻す(設計 00 §5) |
| LLM `complete_structured`(深掘りのレビュー = `anthropic-api` と、指摘を受けた直し) | `[dig].timeout_s`(既定 600s)/呼 | 同一エンジン1回(401/403/404 = キー・権限・モデル ID の誤りはやり直さない) | 失敗しても段の失敗にしない。未レビュー(直しの失敗なら直す前)のまま届け、ノートとカードに明示する(設計 06 §4。GH#153) |
| Telegram 送信 | 30s | 3回、429 は retry_after 秒待ち | 全滅時は §4 の失敗系へ。transport error(read timeout 後等)のリトライで稀に二重送信しうるが、個人用途で実害小のため許容する |
| Telegram long polling | long poll 50s | 即時再接続(連続失敗は30s間隔) | プロセスは KeepAlive が面倒を見る |

- **SSRF ガード(外部 URL を fetch する全経路の共通 HTTP クライアント規約)**: 本文抽出・短縮 URL 展開・RSS 取得を含むすべての外向き HTTP は、**DNS 解決後の接続先 IP が global でなければ接続を拒否する(`is_global` を基本にした allowlist 方式。private / loopback / link-local に加え shared address space 100.64.0.0/10 等のブラックリスト漏れも遮断)**。**リダイレクト追跡の各ホップで再検査**し(悪意ある RSS entry・短縮 URL のリダイレクト先から、常時稼働機の LAN 内・localhost へのプローブを遮断)、接続は**検証済み IP にピン留め**する(URL ホストを IP に書き換え、Host ヘッダ+SNI で元ホスト名を維持。検証と接続で DNS を別々に引くと、1回目 public・2回目 private を返す DNS rebinding で検査を回避できるため)。
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
