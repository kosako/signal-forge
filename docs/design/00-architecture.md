# 設計 00: 全体アーキテクチャ

ステータス: v1(2026-07-05)/ 上位文書: [../PROPOSAL.md](../PROPOSAL.md)(齟齬時は PROPOSAL が優先)

## 1. コンポーネント図

```text
                    ┌─────────────────────────────────────────────┐
                    │ Mac mini(launchd 管理・ローカルファースト)      │
                    │                                             │
 [X API]──────────▶ │ ■ 朝バッチ  intel run(StartCalendarInterval) │
 [RSS 5〜10本]─────▶ │   collect → extract → analyze → digest …(§2)│
                    │      │        │         │         │         │
                    │   sources   documents  analyses  digests    │
                    │   items ────────┴─────────┴─────────┘        │
                    │            SQLite(正本)                     │
                    │                 │              export ───────┼──▶ [Obsidian Vault]
                    │                 │              deliver ──────┼──▶ [Telegram Bot API]
                    │                                             │
 [Telegram 更新]───▶ │ ■ 受信デーモン  intel poll                    │
  (ボタン/URL/       │   (KeepAlive 常駐・long polling)             │
   夜アンケート)      │   → signals / items(manual) / 感想 / 深掘り  │
                    │   内蔵タイマー: 夜アンケート送信・watchdog      │
                    └─────────────────────────────────────────────┘
```

- SQLite が正本。Obsidian は export 先であり DB にしない(理由: 正本の分裂が plan-c 不採用の主因であり、同じ轍を踏まない)。例外は vault にしか無いもの: DeepDive ノート(公開した後は本文を DB に残さない。設計 01 §7)と、ノートに本人が足した手書きのメモ・take(設計 05 §5 で vault を別に保全する)。
- 外部との接続は「X API の読み取り(OAuth 2.0 の認可を含む)」「RSS と記事本文の取得(extract)」「LLM CLI(`codex exec` が自分の API に接続する)」「Telegram の送受信(long polling)」「Vault へのファイル書き込み」に限る。外部に公開する受信ポートは開けない(理由: 自宅常設機に外部公開面を作らない)。例外は 127.0.0.1 に一時的に開く受信だけで、どちらも外部には公開しない: `intel x-auth` の認可の間のコールバック受信(設計 02 §2)と、`intel doctor` #6 のネットワーク遮断の検査に使う canary サーバ(設計 05 §2)。

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
- **受信デーモン** `intel poll`: getUpdates を long polling し、(1) callback(👍🔖✅👎・深掘り追い掛け)→ 検証+signals へ記録後、結果に応じて answerCallbackQuery(記帳成功=記録しました / 検証失敗・期限切れ・DB 失敗など記帳できなかった場合=警告。answer は 1 callback に1回しか返せないため記帳が先。設計 04 §3) (2) URL メッセージ → items(source=manual)へ投入 (3) 夜アンケート回答 → signals へ記録 (4) 深掘りカード・その返事へのリプライ → 感想として記録し、質問として台帳に積んで `intel answer` を別プロセスで起動(返事の送信は子が行う。LLM の待ちで受信を止めないため。GH#126 / 設計 05 §1.2) (5) ダイジェストの記事メッセージ・ヘッダへのリプライ → 感想として item_notes / digest_notes に記録(GH#92 / GH#96) (6) 未消化一覧へのリプライ → 深掘りの指定として dig_queue を更新(GH#104 / 設計 06 §2)。allowlist 外の from.id はログ記録のみで破棄。加えて**内蔵タイマー**で夜アンケート送信(21:30 JST 仮)と watchdog(7:15 JST)、週 1 回のソースの健康診断(日曜の夜アンケートの時刻。GH#150)を実行する(設計 05 §1.2。plist を2枚に抑え、Telegram 送信経路を集約するため。デーモンは「受信+定時送信タイマー」であり、朝バッチ以外の Telegram 送信はここに寄せる)。
- 両プロセスは **SQLite を介してのみ通信**する(理由: IPC を増やさず、片方が死んでももう片方が壊れない)。WAL モードを有効にし、busy_timeout を設定して同時書き込みに耐える。

## 3. モジュール構成と依存方向

```text
app/
  cli.py          # Typer。intel collect/extract/analyze/digest/deliver/dig/answer/poll/run/doctor
                  #   と x-auth(X OAuth 2.0 の認可)/ x-signals(興味シグナルの取込)/ x-insights(週次インサイト)
                  #   poll = 受信デーモン(long polling 常駐+内蔵タイマー)
                  #   answer = 深掘りカードへの質問に答える(通常は poller が起動。GH#126)
                  #   doctor は --purge(期限切れ削除の単独実行)/ --cost(コスト集計の単発表示)を持つ
  run.py          # 朝バッチの段を直列に実行する(§2。段の失敗の局所化・失敗通知)
  collect.py      # collect 段(全ソースから items を取り込む。カーソル・冪等。設計 02 §1)
  purge.py        # purge 段(期限切れ本文の削除。設計 01 §6)
  backup.py       # backup 段(日次バックアップ。設計 05 §5)
  doctor.py       # intel doctor の検査(設計 05 §2)
  config.py       # config.toml + .env の読込・検証(Pydantic Settings 相当の薄い自前)
  db.py           # sqlite3 接続・migrations/ 適用ランナー・薄いリポジトリ層
  models.py       # sources/items/documents/analyses/signals/digests の dataclass/Pydantic
  http.py         # 共通 HTTP(SSRF ガード・接続先のピン留め。設計 05 §8)
  normalize.py    # canonical_url の正規化(u1)と content_hash(h1)。設計 01 §4〜5
  logs.py         # JSON Lines のログ(設計 05 §3)
  jst.py          # 日本時間(JST)と今日の暦日(複数の段が使う。GH#113)
  prompting.py    # プロンプトの sanitize と単一パスのテンプレート置換(分析・トレンド・深掘り・
                  #   返事が共通に使う。設計 03 §3.2・GH#113)
  vault_guard.py  # vault へのアクセスに時間の上限を付ける(返事・take の記録・深掘りのノートの
                  #   公開が使う。GH#145・GH#234)
  source_health.py # 情報ソースの健康(連続の失敗の知らせ・週 1 回の健康診断。読み取りだけ。GH#150)
  connectors/     # base.py(Connector の型)、x.py(ホームTL + XRouter)、x_search.py(AI 検索)、
                  #   x_oauth2.py(OAuth2 PKCE・S1)、x_signals.py(興味シグナル取込・S1b)、rss.py
                  #   — items を produce(カーソル・冪等)
                  #   manual は専用コネクタを持たず、受信デーモンが直接投入(設計 02 §4)
  insights.py     # X 興味シグナルの週次インサイト(読取専用・S1c)
  extraction/     # trafilatura ラッパ — documents を produce(extraction_status 付き)
  analysis/       # llm.py(LLMEngine 抽象層)、fallback.py(主のエンジンが失敗した呼び出しを予備で
                  #   やり直す。GH#156)、stage1.py(事前選別)、analyzer.py(本分析)、
                  #   trend.py(今日の全体トレンド)、profile.py(関心プロファイルの読込)— analyses を produce
  ranking/        # スコア順選抜・最大10件・ネガティブフィルタ後処理
  digest/         # digests(日本語 Markdown)を produce
  delivery/       # telegram.py(Bot API 直叩き・配信)、poller.py(受信デーモン)、
                  #   surveys.py(夜アンケート・watchdog・失敗通知)、state.py(telegram_state KV)、
                  #   answer_worker.py(poller から intel answer を起動する。GH#126)
  dig.py          # 深掘りキュー(夜の入口。GH#104。poller から import — 軽量に保つ)
  deepdive.py     # 朝の調査段(GH#105。DeepDive ノート生成 + 概要カード。設計 06 §4〜5)
  dig_answer.py   # カードへの質問への返事(GH#126。操作面に依存しない。ADR 0005)
  dig_take.py     # カードへのリプライのうち take を解説ノートと followup_dig に記録する(GH#106)
  export/obsidian.py    # Items/ + Daily/ への Markdown 書き出し(DeepDive/ への書き出しも
                  #   同じ atomic_write を使う。DeepDive ノートの中身は deepdive.py が作る)
migrations/       # 001_init.sql 形式、db.py の自前ランナーで適用
prompts/          # バージョン付き .md(analyze・trend・dig・dig-translate・dig-answer・dig-review・
                  #   dig-revise。analyses は prompt_version 列で追跡し、ほかは各モジュールの版の定数。
                  #   PROMPT_VERSION と、deepdive.py の TRANSLATE_・REVIEW_・REVISE_PROMPT_VERSION)
tests/
```

依存方向の規則(理由: 差し替え点を外周に置き、正本ロジックを守る):

- `cli.py`(と朝バッチの `run.py`)→ 各処理モジュール → 共通の層 の一方向。共通の層は `db.py`/`models.py`/`config.py` と、どの段からも使う小さな部品(`jst.py`・`prompting.py`・`vault_guard.py`・`normalize.py`・`http.py`・`logs.py`)。処理モジュール同士は直接 import しない(データの受け渡しは DB 経由。RSS 等の `raw_content` も `items.raw_payload`(設計 01 §6 の JSON 形)経由で extraction に渡す)。複数の段が使う定数や処理は、特定の段の module に置かず共通の層に置く(GH#113)。
- 例外として許している依存(段をまたぐ呼び出しそのものが目的のもの。増やすときはここに書く):
  - 差し替え点の抽象: `connectors/base.py`(コネクタの型とエラー。`http.py`・extraction も使う)と `analysis/llm.py`(LLMEngine。§5)は、どの段からも使う。`analysis/fallback.py`(予備への切り替え。§5)は LLMEngine を作る側(cli・run)が使う
  - 受信デーモン(`delivery/poller.py`)は、受け取った操作を各段の入口に渡す(`dig.py`・`dig_answer.py`。返事の起動は `delivery/answer_worker.py` が `dig_answer.py`・`dig_take.py` の未処理を見て行う)
  - 深掘りの段(`deepdive.py`)は、カードの送信と書き出しに `delivery/telegram.py`・`export/obsidian.py` を使う。深掘りの段と返事(`dig_answer.py`)は、関心プロファイルを `analysis/profile.py` で読む。返事と take の記録(`dig_answer.py`・`dig_take.py`)は、DeepDive の置き場の名前を `deepdive.py` から読む
  - digest 段(`digest/builder.py`)は選抜に `ranking/` を使う(選抜は digest の一部。設計 03 §6)
  - X の興味シグナル(`connectors/x_signals.py`)は、ブックマークを深掘りのキューに積む(`dig.py`)。カードへの返事(`dig_answer.py`)も、調べ直しの依頼を同じ規則でキューに積む(`dig.py`。GH#137)
  - ソースの健康(`source_health.py` の連続の失敗の知らせと週 1 回の健康診断)を、doctor と poller の内蔵タイマー(`delivery/surveys.py`)が使う。🔖 保存節の表示件数(`BOOKMARK_SECTION_MAX`)は、Markdown と Telegram で揃えるため `delivery/telegram.py` が `digest/builder.py` から読む
  - `doctor.py` は検査のために各段を読む
- 外部 I/O(httpx・feedparser・trafilatura・LLM CLI)は connectors/extraction/analysis/delivery の内側に閉じ込め、models の語彙(items, documents, …)に変換してから外へ出す。
- パイプラインはソース非依存: connectors が items さえ作れば、X を止めても RSS+手動 URL だけで全段が動く(撤退・縮退シナリオの前提)。

## 4. 設定・secrets 管理

- **config.toml**: `~/.config/intel/config.toml`(リポジトリ外)。X 読取上限(ホームTL `x.daily_read_cap`・検索 `x_search.daily_read_cap`・合算の単一ノブ `x.combined_daily_read_cap`。ADR 0001)、検索スロット `x_search.slots`(最大3・入れ替え式)、夜アンケート時刻 `telegram.survey_time`・watchdog 時刻 `telegram.watchdog_time`(朝の配信時刻自体は config キーでなく launchd plist の StartCalendarInterval。設計 05 §1.1)、配信件数上限 `digest.max_items`(既定10。「無効日3日連続なら5件に削減」の運用ノブ。設計 03 §6)、深掘り調査段 `[dig]`(日次上限 `daily_cap` 既定2・0=無効、解説生成の `reasoning_effort`、LLM 1呼び出しの `timeout_s`、本文入力上限 `body_max_chars` / 対訳上限 `translation_max_chars`、材料不足しきい値 `min_body_chars`。GH#105 / 設計 06 §3〜4。カードへの質問への返事の effort `answer_reasoning_effort` 既定 low・材料に渡す原文/対訳ノートの上限 `answer_source_max_chars`・1 日に受け付ける質問の上限 `answer_daily_cap` 既定 20(0 = 返事をしない)。GH#126。解説ノートの別系統の AI のレビュー `review` 既定 off・その月の上限額 `review_monthly_cap_usd` 既定 $10。GH#153)、Telegram allowlist(user_id/chat_id)、Vault パス、LLM エンジン指定(§5)、Claude API のモデル・effort・出力上限・単価 `[anthropic_api]`(GH#153)、主のエンジンが失敗したときの予備 `[llm_fallback]`(既定 off・モデル・出力上限・単価・月の上限額。GH#156)、documents TTL(既定30日)、月次コスト上限、DB・バックアップ先パス(`storage.db_path` / `storage.backup_dir`)。**ソース定義(RSS の feed URL・enabled)の正本は DB の sources テーブル**(01 §3。カーソル・死活カウンタと同居させ、config との二重管理・同期を持たない。plan #7 で確定)。ソースの追加・無効化は手動 SQL(週次レビューの棚卸しと同じ操作面)。
- **secrets**: `~/.config/intel/.env`(chmod 600)に X API キー・Telegram bot token。launchd の plist には secrets を書かない(plist は誰でも読めるため)。
- **ANTHROPIC_API_KEY は launchd 実行環境に載せない**(理由: claude -p が API 課金へ無自覚フォールバックするのを設定で遮断)。API エンジン(`anthropic-api`。GH#153)は .env の別名キー `INTEL_ANTHROPIC_API_KEY` だけから読み、AnthropicApiEngine がリクエストのヘッダにだけ載せる(環境変数にはしない)。
- `intel doctor` が検査: .env のパーミッション、token 有効性(getMe)、Vault 書き込み可否(TCC/Full Disk Access)、DB マイグレーション適用状態、月次コストの実績と着地見込み(全項目は設計 05 §2)。

## 5. LLM 抽象層

```python
class LLMEngine(Protocol):
    name: str
    def complete_structured(self, prompt: str, schema: type[BaseModel],
                            timeout_s: int = 120) -> BaseModel: ...
```

- **MVP のスコープ**: LLMEngine Protocol +**第一候補エンジン1本**のみ実装する。**第一候補は `codex-cli` に仮決定(2026-07-06、Issue #3)**: (1) 無人自動化ユースケースを公式ドキュメントが明記し規約リスク最小 (2) 開発作業(Claude Code)とクォータを食い合わない (3) `-o`(output-last-message)/`--output-schema` で構造化出力が素直(15件バッチ実測でフェンスなしクリーン JSON) (4) read-only sandbox+作業ディレクトリ隔離が動的検査で機能。Week 1 の安定性実測(#9/#14)で最終確定する。複数エンジンの優先順リスト(`llm.engines`)・エンジンフォールバック・残り2実装(もう片方の CLI と `AnthropicApiEngine`)は**拡張点に降格**し、必要になった時点で追加する(理由: 1〜1.5週の工期に3実装+フォールバック網はテスト面と doctor 検査を3倍にする過剰装備。Protocol があるので後付けコストは低く、PROPOSAL の「API SDK へ1日で差し替え可能」要件は Protocol で満たす)。**`AnthropicApiEngine`(`anthropic-api`)は 2026-10 に深掘りノートのレビュー役として実装した**(GH#153。別系統の AI に元記事と照らして指摘させる。主経路の `llm.engine` にはまだ配線しない)。GH#156 で、主のエンジンが失敗した呼び出しの予備にも使う(下の「予備への切り替え」。優先順リスト `llm.engines` ではなく、主 1 本 + 予備 1 本の形)。`claude-cli` は未実装(拡張点のまま)。
- **切替**: config.toml の `llm.engine = "codex-cli"`(単一指定。config が受け付ける値は `codex-cli` / `claude-cli` / `anthropic-api`。01 の analyses.engine enum と同一語彙)。**主のエンジンとして実装しているのは `codex-cli` だけ**で、ほかの値は config の検証は通るが、エンジンを作る時点(`create_engine`)で失敗する(doctor #6 も FAIL)。主経路を差し替えるには、そのエンジンの実装と `create_engine` への配線が要る。推論専用モードを設定できないエンジンは起動時に doctor が fail させる(理由: 「LLM に書込・送信権限を与えない」を宣言でなく構成で担保)。深掘り調査段(GH#105 / 設計 06 §4)も**同一エンジン・同一隔離**で呼び出す(effort のみ `[dig].reasoning_effort` で可変。vault 書込・Telegram 送信は LLM でなく orchestrator 側 Python の責務)。
- **呼び出しの隔離仕様(推論専用モードの実体)**。「ツール disallow」の宣言だけでは、ユーザーの設定・MCP 由来ツールが生き残りうるため、以下を構成で強制する:
  1. `claude -p` は**専用の設定分離**で起動する: `--setting-sources ""` で user/project/local 設定(CLAUDE.md・settings.json)を読み込ませず、`--disallowedTools "*"` + `--strict-mcp-config`(MCP 空指定)でツールを全遮断する。**専用 CONFIG DIR/HOME 分離は使わない**(実測 2026-07-06、Issue #3: CONFIG DIR を分けると認証も分離され Not logged in で不成立)。出力は ```json フェンス付きで返るため、スキーマ検証前にフェンス除去を行う。
  2. `codex exec` は**作業ディレクトリを空の専用ディレクトリに固定**し、ネットワーク遮断サンドボックスで起動する(read-only でもファイル読み取りは可能なため、`.env`・SSH 鍵等が summary 経由で出力に流出する経路を作業ディレクトリ隔離で塞ぐ)。**認証は使い捨て CODEX_HOME(auth.json symlink のみ)で最小限見せる**が、read-only sandbox は同居する auth.json を summary 経由で流出させうる(Issue #54 で実測。symlink 経由の実体読取・絶対パス直読みを含む)。これを **permission profile の deny-read** で塞ぐ: `-s read-only` の代わりに `default_permissions` で名前付き profile を選び(`-s` を明示すると profile の filesystem deny が無視される・実測)、`[permissions.<name>].filesystem` に write を一切与えず network を空にして read-only 相当の隔離を保ったまま、**CODEX_HOME と実 `~/.codex` の読取を deny** する。codex 自身の auth 読取は sandbox 外で行われるため認証は維持される。
  2b. `codex exec` の既定で有効な、外へ届く・実行するツールの機能(アカウント側の接続サービス・Web・ブラウザ・画像生成・shell・コード実行・サブエージェント等)を `--disable` で機能ごと外し、Web 検索も `web_search="disabled"` にする(GH#129)。接続サービスや Web のツールは sandbox の外で動くため、2 の書込・network 遮断では止まらない(2026-10-18 に実測。無効化前は多数のツールが見えていた)。`--strict-config` の下で未知の feature 名は起動エラーになる(黙って無視されない)。codex が起動引数を拒否したとき(版の不一致)は環境障害(`LLMUnavailableError`)として扱い、記事を degraded で進めない(GH#74 と同じ扱い)。拒否かどうかは stderr の書き出しを読んで決めるだけで、例外には当たった決まった書き出しだけを載せ、stderr の中身は載せない(GH#202)。
  3. doctor の推論専用モード検査は設定値の確認でなく**動的検査**とする: 隔離が効いているかを canary で確かめる 3 つの検査(HOME の隔離・ネットワーク遮断・CODEX_HOME の隔離)。read-only の sandbox は読み取りを許すので、「読み取りを試みて拒否されることを確認する」方式は成り立たない(実測。Issue #3)。詳細は設計 05 §2 #6。
  4. プロンプトは **argv でなく stdin 渡し**(ps でのプロンプト露出防止と、シェル解釈の事故防止を兼ねる)。
  5. `anthropic-api` は素の Messages API で呼び、リクエストに tools・MCP・コンテナを**一切付けない**(モデルが読む・書く・取りに行く手段を構造的に持たない。CLI と違い、塞ぐべき既定のツールが無い)。宛先は API の URL に固定し、プロキシの環境変数は読まない。キーは .env の別名キーだけから読み、印字可能な ASCII 以外を含むキーは送る前に止める。通信の失敗は元の例外をつながずに正規化する(通信の例外のメッセージはヘッダの値 = キーを含みうり、つなぐと traceback 経由でログに出るため)。応答の検証(JSON・スキーマ)の失敗も、応答の本文(外部の記事の文を含みうる)を載せず、合わない場所と種類だけを返す(GH#156)。モデル・effort・出力の上限(思考 + 本文)・単価は config `[anthropic_api]`(既定 Opus 5.5・medium)。応答ごとに使ったトークン数と見積もり額を返し、打ち切り(`max_tokens`)・拒否(`refusal`)は失敗にする。認証・権限・モデル ID の誤り(401/403/404)は環境障害(`LLMUnavailableError`)、それ以外の失敗は一時的な失敗(同一エンジンで 1 回やり直す)。
- **呼び出し規約**: 1呼び出し10〜20件のバッチ判定。subprocess 実行、timeout 120s。`codex exec` は別セッションで起動し、timeout でも中断(手動実行の Ctrl-C)・例外でも、孫を含むプロセスグループを止めて回収してから使い捨ての作業ディレクトリを消す(GH#244。別セッションには端末の Ctrl-C が届かないため)。出力は Pydantic でスキーマ検証(フィールド allowlist・スコア値域・理由の長さ上限)。
- **失敗時**: 検証失敗またはタイムアウト → 同一エンジンで1リトライ(失敗はチャンク単位に局所化し、該当チャンクのみ再実行)→ なお失敗ならタイトルのみの degraded ダイジェストへ直行(理由: 無配信より劣化配信。黙って止まらない)。**例外(GH#74)**: 環境障害(エンジン起動不能・認証不在 = `LLMUnavailableError`)はリトライ・degraded で回復しないため、per-chunk で握らず analyze 段の失敗として raise する(silent full-degradation 防止。#72 の実障害)。**失敗の例外の文言(GH#202)**: 各段がログと DB に書くので、`codex-cli` も 5 の `anthropic-api` と同じく失敗の種類・合わない場所(JSON の位置・スキーマの項目と検証の種類)・終了コードだけを載せ、応答の本文・stderr の中身・本文を持つ原因の例外は載せない(設計 05 §3)。
- **予備への切り替え(GH#156。config `[llm_fallback]`、既定 off)**: 主のエンジン(Codex)が失敗した呼び出しだけを、予備(`anthropic-api`。既定 Sonnet 5.5)でやり直す(`analysis/fallback.py` の FallbackEngine)。使い分けは「残量を見て切り替える」ではなく「落ちたら次へ」で、主の失敗そのものを合図にする(サブスクの残量は外から読めない。GH#153 の相談)。
  - 主が続けて 2 回失敗したら(起動できない = `LLMUnavailableError` は 1 回で)、同じ factory から作るエンジンは主を飛ばして予備へ行く(Codex が止まった朝に、チャンクごとに主の失敗を待たない)。主に戻るのは次のプロセスから(朝バッチは毎朝、返事は質問ごとに起動し直す)。
  - 予備も失敗したら、**主の失敗と同じ種類のエラー**にする(LLMError なら劣化、LLMUnavailableError なら段の失敗、という各段の扱いを変えない)。予備を作れない(キーが無い)・月の上限に達したときも同じ。そのエラーのメッセージは主の失敗の種類だけにする(各段がログに書くため。主の例外の全文は外部の文を含みうる)。LLM の失敗以外の例外(実装の誤り)は予備に回さない。ログには主の失敗の種類(例外の型と、`codex 非0終了(1)`・`タイムアウト` などメッセージの書き出しの決まった語)だけを書き、続く本文(codex の stderr の末尾・応答の先頭。外部の文を含みうる)は書かない。
  - effort は主の effort に合わせる(codex の minimal は Claude の low)。`name` / `model` は最後に答えたエンジンのものにする(呼び出しの直後に読めば、その呼び出しに答えたエンジン)。analyzer は結果ごとにそれを控えて analyses の engine / model に記録する(1 つのチャンクの中で、バッチは主・欠けた item の単発の再分析は予備、と混ざりうるため)。
  - 対象は、主のエンジンを使う全経路: analyze・トレンド・深掘り(解説・対訳・指摘を受けた直し)・カードへの返事(朝バッチの `run.py` と、`cli.py` の analyze / digest / dig / answer)。深掘りのレビュー役(GH#153。もともと Claude API)は対象外。段ごとに factory を分けるので、主を飛ばす判定と台帳の段は段ごと。朝バッチの analyze・トレンドは、`run_pipeline` に渡された主の作り方をそのまま包む(テストで主を差し替えられる)。
  - 予備で答えられても Codex が止まっていることには変わらないので、朝バッチの後に 1 通知らせる(設計 05 §4。段ごとの回数・予備も失敗した呼び出しの回数・今回と今月の額。呼び出しの回数なので、段の中のやり直しで答えられた分も含む)。朝バッチの外(カードへの返事)の利用は `answer.log` の WARNING と台帳・doctor #9 で見る。
  - 予備の呼び出しは台帳 `llm_fallback_calls`(migration 018。予備も失敗した呼び出しも課金されうるので残す)に記録し、月(UTC の暦月。doctor #9 の当月と同じ)の合計が `[llm_fallback].monthly_cap_usd`(既定 $10)に達したら予備を使わない(判定は呼び出しの前・記録は後なので、始めた呼び出しの分だけ超えうる。朝バッチと返事は同時に走りうる)。費用は doctor #9 に入る。
- 外部本文はプロンプト内で明示フェンス(ランダム nonce 区切り。設計 03 §3.2)で囲み、data として扱う旨を毎回指示。title・author 等のメタデータも未検証データとしてサニタイズする(同 §3.2)。配信・ノートに載せる URL は LLM 出力でなく DB 上の正規 URL のみ。

## 6. エラーハンドリング方針

原則: **段ごとに局所化し、進めるものは進め、止まったことは必ず通知する**。

| 障害 | 挙動 |
|---|---|
| 単一ソースの collect 失敗 | sources に失敗時刻を記録し他ソースは続行。連続失敗は doctor #14 が警告し、X は watchdog も通知(設計 02 §2・§3) |
| 本文抽出失敗 | extraction_status=failed のままタイトル+メタで degraded 分析に回す(黙って脱落させない) |
| LLM 失敗(transient: 検証失敗・タイムアウト) | §5 のリトライ→degraded ダイジェスト |
| LLM 環境障害(起動不能・認証不在)・分析全滅(analyzed=0) | degraded で握らず analyze 段の失敗として raise → 失敗通知(設計 05 §4)。全滅時は degraded 行が書き込み済みのため劣化配信は併走する(GH#74)。digest は当日(JST)に分析された候補だけで全滅を判定し、劣化配信にも当日の分だけを載せる(閾値に届かず analyzed のまま残る過去の記事に当日の全滅を隠させない。過去の記事は候補のまま残す。GH#175) |
| Telegram 送信失敗 | 指数バックオフで3回。export は deliver より前段(§2 の順序)なので Obsidian への蓄積は完了済み。全滅時は `delivery_status='failed'` のまま終了し、再走時に digest_messages 台帳で未送信のみ再送(SQLite が正本なので再送可能) |
| Telegram 完全未設定 | deliver 段を skip(WARN ログ)。M2a 前の未セットアップ状態を段失敗にしない。欠落検知は doctor #4 の責務(設計 05 §2) |
| 朝バッチ全体の異常終了 | watchdog(受信デーモン `intel poll` の内蔵タイマー。7:15 JST に digests の当日行を確認)が未生成を検知し Telegram に通知 |
| 受信デーモン死亡 | launchd KeepAlive が再起動。getUpdates は offset カーソルで再開(取りこぼしなし) |

冪等性: collect はソース別カーソル+content_hash 重複排除で再実行安全。`intel run` の再実行は当日 digest を再生成せず、**未配信(`delivery_status ≠ 'sent'`)の場合のみ送信し、配信済みなら skip する**(理由: 二重配信で KPI 分母を汚さない)。送信途中失敗時の「未送信アイテムのみ再送」は設計 04 §2(digest_messages 台帳)に従う。

## 7. 拡張点(設計はしない、口だけ確保)

- **フェーズ2 深掘り(実装済み。正本は設計 06)**: 深掘りキュー `dig_queue`(migrations/009)と調査結果の台帳 `dig_results`(migrations/010)で実装した。調査は別プロセスでなく朝バッチの dig 段(§2)で消化する。履歴: 当初は 🔖 signal を入力に `research_tasks` テーブルを追加し、受信デーモンがキュー投入・別プロセスが消化する形を想定していた(既存テーブルへの変更が不要になるよう、signals は item_id+種別+記録日時を正規化済み)。
- **ランキング自動調整**: analyses(プロンプト ver・入力 hash)と signals が揃っているため、将来のフィードバックループは読み取り専用の新モジュール追加で足りる。
- **サーバー移行**: launchd への依存は plist 2枚に閉じており、cron/systemd への置換で済む。SQLite・ファイル設定・long polling はそのまま可搬。Obsidian export だけがローカルパス前提のため、移行時は Vault 同期方式の再検討が必要——それ以上の抽象化は今はしない(理由: YAGNI、Mac mini 運用が当面の正)。
