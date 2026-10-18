# 設計 00: 全体アーキテクチャ

ステータス: v1(2026-07-05)/ 上位文書: [../PROPOSAL.md](../PROPOSAL.md)(齟齬時は PROPOSAL が優先)

## 1. コンポーネント図

```text
                    ┌─────────────────────────────────────────────┐
                    │ Mac mini(launchd 管理・ローカルファースト)      │
                    │                                             │
 [X API]──────────▶ │ ■ 朝バッチ  intel run(StartCalendarInterval) │
 [RSS 5〜10本]─────▶ │   collect → extract → analyze → digest      │
                    │      │        │         │         │         │
                    │   sources   documents  analyses  digests    │
                    │   items ────────┴─────────┴─────────┘        │
                    │            SQLite(正本)                     │
                    │                 │              export ───────┼──▶ [Obsidian Vault]
                    │                 │              deliver ──────┼──▶ [Telegram Bot API]
                    │                                             │
 [Telegram 更新]───▶ │ ■ 受信デーモン  intel poll                    │
  (ボタン/URL/       │   (KeepAlive 常駐・long polling)             │
   夜アンケート)      │   → signals / items(manual) に記録            │
                    │   内蔵タイマー: 夜アンケート送信・watchdog      │
                    └─────────────────────────────────────────────┘
```

- SQLite が唯一の正本。Obsidian は export 先であり DB にしない(理由: 正本の分裂が plan-c 不採用の主因であり、同じ轍を踏まない)。
- 外部との接続は「X API・RSS の読み取り」「Telegram の送受信(long polling)」「Vault へのファイル書き込み」の3種のみ。受信ポートは一切開けない(理由: 自宅常設機に外部公開面を作らない)。

## 2. プロセスモデル

### 比較: Telegram 反応の受信方式

| 観点 | 常駐ロングポーラー | バッチ時取込(getUpdates を朝バッチ等で周期実行) |
|---|---|---|
| callback 応答 UX | ボタン押下に即 answerCallbackQuery でき、スピナーが即消える | callback query は約10秒で失効するため、周期実行では**構造的に応答不能**(ボタンが押しても反応しない見た目になる) |
| 手動 URL 投入 | 投入後すぐ ACK を返せる | 次回バッチまで無反応 |
| 夜アンケート | 回答を即時記録 | 同上の遅延 |
| 実装・運用コスト | プロセスが1本増える(launchd KeepAlive) | 追加プロセス不要 |

**採用: 常駐ロングポーラー(2プロセス構成)**。理由: 反応ボタンは KPI 測定の根幹であり、callback 失効で「押せないボタン」になる方式は UX 以前に企画要件を満たさない。

### 2プロセスの責務分離

- **朝バッチ** `intel run`: **collect → x_signals(S1b 興味シグナル取込。`[x_signals].daily_read_cap > 0` のときのみ)→ extract → analyze → digest → export(Obsidian)→ deliver(配信)→ dig(深掘り調査。`[dig].daily_cap > 0` のときのみ。GH#105 / 設計 06)→ purge(期限切れ documents / raw_payload / x_signals 本文抜粋の削除)→ backup(日次バックアップ。設計 05 §5「朝バッチ末尾」)** を直列実行し終了(**本節がステージ順序の正本**。05・plan は本順序に従う)。dig を deliver の後に置く理由: ダイジェスト配信を深掘り生成(分オーダー)で遅らせない。purge より前に置く理由: X 由来 documents の TTL 失効前に本文を材料に使う。段の失敗はその段で捕捉して記録し、後続の段は継続する(失敗の局所化。§6)。export を deliver より先に置く理由: Telegram 失敗でも Obsidian への蓄積は完了させる(PROPOSAL のエラー方針)。deliver 失敗時は export 済みのまま `digests.delivery_status='failed'` とし、再送は digest_messages 台帳(設計 04 §2)による未送信のみ再送。launchd StartCalendarInterval(配信時刻前、pmset wake 併設)。
- **受信デーモン** `intel poll`: getUpdates を long polling し、(1) callback(👍🔖✅👎・深掘り追い掛け)→ 検証+signals へ記録後、結果に応じて answerCallbackQuery(記帳成功=記録しました / 検証失敗・期限切れ・DB 失敗など記帳できなかった場合=警告。answer は 1 callback に1回しか返せないため記帳が先。設計 04 §3) (2) URL メッセージ → items(source=manual)へ投入 (3) 夜アンケート回答 → signals へ記録 (4) 深掘りカード・その返事へのリプライ → 感想として記録し、質問として台帳に積んで `intel answer` を別プロセスで起動(返事の送信は子が行う。LLM の待ちで受信を止めないため。GH#126 / 設計 05 §1.2)。allowlist 外の from.id はログ記録のみで破棄。加えて**内蔵タイマー**で夜アンケート送信(21:30 JST 仮)と watchdog(7:15 JST)を実行する(設計 05 §1.2。plist を2枚に抑え、Telegram 送信経路を集約するため。デーモンは「受信+定時送信タイマー」であり、朝バッチ以外の Telegram 送信はここに寄せる)。
- 両プロセスは **SQLite を介してのみ通信**する(理由: IPC を増やさず、片方が死んでももう片方が壊れない)。WAL モードを有効にし、busy_timeout を設定して同時書き込みに耐える。

## 3. モジュール構成と依存方向

```text
app/
  cli.py          # Typer。intel collect/extract/analyze/digest/deliver/dig/poll/run/doctor
                  #   poll = 受信デーモン(long polling 常駐+内蔵タイマー)
                  #   doctor は --purge(期限切れ削除の単独実行)/ --cost(コスト集計の単発表示)を持つ
  config.py       # config.toml + .env の読込・検証(Pydantic Settings 相当の薄い自前)
  db.py           # sqlite3 接続・migrations/ 適用ランナー・薄いリポジトリ層
  models.py       # sources/items/documents/analyses/signals/digests の dataclass/Pydantic
  connectors/     # x.py(ホームTL + XRouter), x_search.py(AI 検索), x_oauth2.py(OAuth2 PKCE・S1), x_signals.py(興味シグナル取込・S1b), rss.py
  insights.py     # X 興味シグナルの週次インサイト(読取専用・S1c)
                  #   — items を produce(カーソル・冪等)
                  #   manual は専用コネクタを持たず、受信デーモンが直接投入(設計 02 §4)
  extraction/     # trafilatura ラッパ — documents を produce(extraction_status 付き)
  analysis/       # llm.py(LLMEngine 抽象層)+ analyzer.py — analyses を produce
  ranking/        # スコア順選抜・最大10件・ネガティブフィルタ後処理
  digest/         # digests(日本語 Markdown)を produce
  delivery/       # telegram.py(Bot API 直叩き・配信)、poller.py(受信デーモン)、
                  #   surveys.py(夜アンケート・watchdog・失敗通知)、state.py(telegram_state KV)
  dig.py          # 深掘りキュー(夜の入口。GH#104。poller から import — 軽量に保つ)
  deepdive.py     # 朝の調査段(GH#105。DeepDive ノート生成 + 概要カード。設計 06 §4〜5)
  export/obsidian.py    # Items/ + Daily/ への Markdown 書き出し
migrations/       # 001_init.sql 形式、db.py の自前ランナーで適用
prompts/          # バージョン付き .md(analyses.prompt_ver で追跡)
tests/
```

依存方向の規則(理由: 差し替え点を外周に置き、正本ロジックを守る):

- `cli.py` → 各処理モジュール → `db.py`/`models.py`/`config.py` の一方向のみ。処理モジュール同士は直接 import しない(データの受け渡しは DB 経由。RSS 等の `raw_content` も `items.raw_payload`(設計 01 §6 の JSON 形)経由で extraction に渡す)。
- 外部 I/O(httpx・feedparser・trafilatura・LLM CLI)は connectors/extraction/analysis/delivery の内側に閉じ込め、models の語彙(items, documents, …)に変換してから外へ出す。
- パイプラインはソース非依存: connectors が items さえ作れば、X を止めても RSS+手動 URL だけで全段が動く(撤退・縮退シナリオの前提)。

## 4. 設定・secrets 管理

- **config.toml**: `~/.config/intel/config.toml`(リポジトリ外)。X 読取上限(ホームTL `x.daily_read_cap`・検索 `x_search.daily_read_cap`・合算の単一ノブ `x.combined_daily_read_cap`。ADR 0001)、検索スロット `x_search.slots`(最大3・入れ替え式)、夜アンケート時刻 `telegram.survey_time`・watchdog 時刻 `telegram.watchdog_time`(朝の配信時刻自体は config キーでなく launchd plist の StartCalendarInterval。設計 05 §1.1)、配信件数上限 `digest.max_items`(既定10。「無効日3日連続なら5件に削減」の運用ノブ。設計 03 §6)、深掘り調査段 `[dig]`(日次上限 `daily_cap` 既定2・0=無効、解説生成の `reasoning_effort`、LLM 1呼び出しの `timeout_s`、本文入力上限 `body_max_chars` / 対訳上限 `translation_max_chars`、材料不足しきい値 `min_body_chars`。GH#105 / 設計 06 §3〜4。カードへの質問への返事の effort `answer_reasoning_effort` 既定 low・材料に渡す原文/対訳ノートの上限 `answer_source_max_chars`・1 日に受け付ける質問の上限 `answer_daily_cap` 既定 20(0 = 返事をしない)。GH#126)、Telegram allowlist(user_id/chat_id)、Vault パス、LLM エンジン指定(§5)、documents TTL(既定30日)、月次コスト上限、DB・バックアップ先パス(`storage.db_path` / `storage.backup_dir`)。**ソース定義(RSS の feed URL・enabled)の正本は DB の sources テーブル**(01 §3。カーソル・死活カウンタと同居させ、config との二重管理・同期を持たない。plan #7 で確定)。ソースの追加・無効化は手動 SQL(週次レビューの棚卸しと同じ操作面)。
- **secrets**: `~/.config/intel/.env`(chmod 600)に X API キー・Telegram bot token。launchd の plist には secrets を書かない(plist は誰でも読めるため)。
- **ANTHROPIC_API_KEY は launchd 実行環境に載せない**(理由: claude -p が API 課金へ無自覚フォールバックするのを設定で遮断)。API エンジンを明示使用する場合のみ .env に別名キーで持ち、AnthropicApiEngine 内でだけ環境へ渡す。
- `intel doctor` が検査: .env のパーミッション、token 有効性(getMe)、Vault 書き込み可否(TCC/Full Disk Access)、DB マイグレーション適用状態、月次コスト見込みとクォータ消費率。

## 5. LLM 抽象層

```python
class LLMEngine(Protocol):
    name: str
    def complete_structured(self, prompt: str, schema: type[BaseModel],
                            timeout_s: int = 120) -> BaseModel: ...
```

- **MVP のスコープ**: LLMEngine Protocol +**第一候補エンジン1本**のみ実装する。**第一候補は `codex-cli` に仮決定(2026-07-06、Issue #3)**: (1) 無人自動化ユースケースを公式ドキュメントが明記し規約リスク最小 (2) 開発作業(Claude Code)とクォータを食い合わない (3) `-o`(output-last-message)/`--output-schema` で構造化出力が素直(15件バッチ実測でフェンスなしクリーン JSON) (4) read-only sandbox+作業ディレクトリ隔離が動的検査で機能。Week 1 の安定性実測(#9/#14)で最終確定する。複数エンジンの優先順リスト(`llm.engines`)・エンジンフォールバック・残り2実装(もう片方の CLI と `AnthropicApiEngine`)は**拡張点に降格**し、必要になった時点で追加する(理由: 1〜1.5週の工期に3実装+フォールバック網はテスト面と doctor 検査を3倍にする過剰装備。Protocol があるので後付けコストは低く、PROPOSAL の「API SDK へ1日で差し替え可能」要件は Protocol で満たす)。
- **切替**: config.toml の `llm.engine = "codex-cli"`(単一指定。値は `codex-cli` / `claude-cli` / `anthropic-api`。01 の analyses.engine enum と同一語彙)。コード変更なしで差し替え可能。推論専用モードを設定できないエンジンは起動時に doctor が fail させる(理由: 「LLM に書込・送信権限を与えない」を宣言でなく構成で担保)。深掘り調査段(GH#105 / 設計 06 §4)も**同一エンジン・同一隔離**で呼び出す(effort のみ `[dig].reasoning_effort` で可変。vault 書込・Telegram 送信は LLM でなく orchestrator 側 Python の責務)。
- **呼び出しの隔離仕様(推論専用モードの実体)**。「ツール disallow」の宣言だけでは、ユーザーの設定・MCP 由来ツールが生き残りうるため、以下を構成で強制する:
  1. `claude -p` は**専用の設定分離**で起動する: `--setting-sources ""` で user/project/local 設定(CLAUDE.md・settings.json)を読み込ませず、`--disallowedTools "*"` + `--strict-mcp-config`(MCP 空指定)でツールを全遮断する。**専用 CONFIG DIR/HOME 分離は使わない**(実測 2026-07-06、Issue #3: CONFIG DIR を分けると認証も分離され Not logged in で不成立)。出力は ```json フェンス付きで返るため、スキーマ検証前にフェンス除去を行う。
  2. `codex exec` は**作業ディレクトリを空の専用ディレクトリに固定**し、ネットワーク遮断サンドボックスで起動する(read-only でもファイル読み取りは可能なため、`.env`・SSH 鍵等が summary 経由で出力に流出する経路を作業ディレクトリ隔離で塞ぐ)。**認証は使い捨て CODEX_HOME(auth.json symlink のみ)で最小限見せる**が、read-only sandbox は同居する auth.json を summary 経由で流出させうる(Issue #54 で実測。symlink 経由の実体読取・絶対パス直読みを含む)。これを **permission profile の deny-read** で塞ぐ: `-s read-only` の代わりに `default_permissions` で名前付き profile を選び(`-s` を明示すると profile の filesystem deny が無視される・実測)、`[permissions.<name>].filesystem` に write を一切与えず network を空にして read-only 相当の隔離を保ったまま、**CODEX_HOME と実 `~/.codex` の読取を deny** する。codex 自身の auth 読取は sandbox 外で行われるため認証は維持される。
  2b. `codex exec` の既定で有効な、外へ届く・実行するツールの機能(アカウント側の接続サービス・Web・ブラウザ・画像生成・shell・コード実行・サブエージェント等)を `--disable` で機能ごと外し、Web 検索も `web_search="disabled"` にする(GH#129)。接続サービスや Web のツールは sandbox の外で動くため、2 の書込・network 遮断では止まらない(2026-10-18 に実測。無効化前は多数のツールが見えていた)。`--strict-config` の下で未知の feature 名は起動エラーになる(黙って無視されない)。codex が起動引数を拒否したとき(版の不一致)は環境障害(`LLMUnavailableError`)として扱い、記事を degraded で進めない(GH#74 と同じ扱い)。
  3. doctor の推論専用モード検査は設定値の確認でなく**動的検査**とする: 実際にツール呼び出し・ファイル読み取りを試みるプロンプトを流し、拒否されることを確認する(設計 05 §2 #6)。
  4. プロンプトは **argv でなく stdin 渡し**(ps でのプロンプト露出防止と、シェル解釈の事故防止を兼ねる)。
- **呼び出し規約**: 1呼び出し10〜20件のバッチ判定。subprocess 実行、timeout 120s。出力は Pydantic でスキーマ検証(フィールド allowlist・スコア値域・理由の長さ上限)。
- **失敗時**: 検証失敗またはタイムアウト → 同一エンジンで1リトライ(失敗はチャンク単位に局所化し、該当チャンクのみ再実行)→ なお失敗ならタイトルのみの degraded ダイジェストへ直行(理由: 無配信より劣化配信。黙って止まらない)。エンジンフォールバックは複数エンジン導入時の拡張点。**例外(GH#74)**: 環境障害(エンジン起動不能・認証不在 = `LLMUnavailableError`)はリトライ・degraded で回復しないため、per-chunk で握らず analyze 段の失敗として raise する(silent full-degradation 防止。#72 の実障害)。
- 外部本文はプロンプト内で明示フェンス(ランダム nonce 区切り。設計 03 §3.2)で囲み、data として扱う旨を毎回指示。title・author 等のメタデータも未検証データとしてサニタイズする(同 §3.2)。配信・ノートに載せる URL は LLM 出力でなく DB 上の正規 URL のみ。

## 6. エラーハンドリング方針

原則: **段ごとに局所化し、進めるものは進め、止まったことは必ず通知する**。

| 障害 | 挙動 |
|---|---|
| 単一ソースの collect 失敗 | sources に失敗時刻を記録し他ソースは続行。連続失敗は doctor #14 が警告し、X は watchdog も通知(設計 02 §2・§3) |
| 本文抽出失敗 | extraction_status=failed のままタイトル+メタで degraded 分析に回す(黙って脱落させない) |
| LLM 失敗(transient: 検証失敗・タイムアウト) | §5 のリトライ→degraded ダイジェスト |
| LLM 環境障害(起動不能・認証不在)・分析全滅(analyzed=0) | degraded で握らず analyze 段の失敗として raise → 失敗通知(設計 05 §4)。全滅時は degraded 行が書き込み済みのため劣化配信は併走する(GH#74) |
| Telegram 送信失敗 | 指数バックオフで3回。export は deliver より前段(§2 の順序)なので Obsidian への蓄積は完了済み。全滅時は `delivery_status='failed'` のまま終了し、再走時に digest_messages 台帳で未送信のみ再送(SQLite が正本なので再送可能) |
| Telegram 完全未設定 | deliver 段を skip(WARN ログ)。M2a 前の未セットアップ状態を段失敗にしない。欠落検知は doctor #4 の責務(設計 05 §2) |
| 朝バッチ全体の異常終了 | watchdog(受信デーモン `intel poll` の内蔵タイマー。7:15 JST に digests の当日行を確認)が未生成を検知し Telegram に通知 |
| 受信デーモン死亡 | launchd KeepAlive が再起動。getUpdates は offset カーソルで再開(取りこぼしなし) |

冪等性: collect はソース別カーソル+content_hash 重複排除で再実行安全。`intel run` の再実行は当日 digest を再生成せず、**未配信(`delivery_status ≠ 'sent'`)の場合のみ送信し、配信済みなら skip する**(理由: 二重配信で KPI 分母を汚さない)。送信途中失敗時の「未送信アイテムのみ再送」は設計 04 §2(digest_messages 台帳)に従う。

## 7. 拡張点(設計はしない、口だけ確保)

- **フェーズ2 深掘り(実装済み。正本は設計 06)**: 深掘りキュー `dig_queue`(migrations/009)と調査結果の台帳 `dig_results`(migrations/010)で実装した。調査は別プロセスでなく朝バッチの dig 段(§2)で消化する。履歴: 当初は 🔖 signal を入力に `research_tasks` テーブルを追加し、受信デーモンがキュー投入・別プロセスが消化する形を想定していた(既存テーブルへの変更が不要になるよう、signals は item_id+種別+記録日時を正規化済み)。
- **ランキング自動調整**: analyses(プロンプト ver・入力 hash)と signals が揃っているため、将来のフィードバックループは読み取り専用の新モジュール追加で足りる。
- **サーバー移行**: launchd への依存は plist 2枚に閉じており、cron/systemd への置換で済む。SQLite・ファイル設定・long polling はそのまま可搬。Obsidian export だけがローカルパス前提のため、移行時は Vault 同期方式の再検討が必要——それ以上の抽象化は今はしない(理由: YAGNI、Mac mini 運用が当面の正)。
