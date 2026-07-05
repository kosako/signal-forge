# Personal Intelligence Service — Agent Handoff Memo

作成日: 2026-07-05  
目的: これまでの会話内容を、別エージェントが設計・実装・追加壁打ちに引き継げる形で要約する。

---

## 1. 背景とユーザーの意図

ユーザーは、自分用の情報収集・整理サービスを作りたい。将来的には、過去に構想していた「自分用の情報収集ツール／知的作業支援ツール」に統合してもよいが、情報収集機能は独立サービスとして存在してもよいと考えている。

主な対象は以下。

- ユーザー自身のSNS周辺の情報
- 一般的なニュース
- RSSやWeb記事
- 将来的には任意URLや複数SNSも対象候補

ユーザーが求めているのは、単なるスクレイピングやアーカイブではなく、収集した情報をある程度解析し、要約・整理・推薦・示唆出しまで行う個人用インテリジェンス基盤である。

重要な仮説:

> ユーザーが本当に欲しいのは「情報の保存庫」ではなく、「今日見るべき情報を選び、その理由と自分への意味を説明してくれる仕組み」。

---

## 2. 現時点の推奨方針

結論として、初期版は以下の方針が最も筋がよい。

> ローカルファーストで作り、後からサーバーへ移せる単一アプリとして設計する。

初期構成:

- Python中心
- SQLiteを正本DBにする
- SQLite FTSなどで全文検索
- CLIで収集・分析・出力を実行
- ObsidianにはMarkdownを書き出す
- Dockerfileは用意するが、最初はローカル実行
- 後から同じアプリをDockerでサーバーへ移せるようにする

初期段階で避けるべきこと:

- 最初からWebアプリ化する
- 最初からObsidianプラグインを作る
- 最初からベクトルDBやRAGを入れる
- 最初から複数SNSに対応する
- Obsidianを正本DBとして扱う
- Local REST APIなどを前提に複雑な連携を組む

---

## 3. ローカル vs サーバーの整理

### 3.1 初期判断

初期版はローカルでよい。

理由:

- 機能的には常時公開サーバーである必要が薄い
- ユーザーはClaude CodeやCodex等を使って開発したい可能性があり、ローカルリポジトリで開発しやすい
- Obsidian VaultへのMarkdown出力と相性がよい
- 個人用サービスなので、まず価値検証を優先すべき
- サーバー化すると、認証、同期、監視、デプロイ、秘密情報管理、障害対応が増える

ただし、「Claude Code / Codexを使うなら本番もローカルでなければならない」という前提は誤り。開発対象のコードがローカルにあればよく、稼働先はサーバーでも構わない。

### 3.2 サーバーへ移す判断基準

以下のいずれかが発生したら、サーバー化を検討する。

- PC停止やスリープにより収集漏れが週2回以上起きる
- 朝など決まった時刻に必ずダイジェストを生成したい
- Webhook受信が必要になる
- 外出先から即時検索・確認したくなる
- ローカル実行が普段の作業を邪魔する
- ローカル端末またはサーバー上のどちらにトークンを置くか、セキュリティ上の明確な理由が出る
- 複数端末から同じ状態を扱いたくなる

### 3.3 実行方式

アプリ内に常駐スケジューラを持たせるより、OS側のスケジューラでCLIを定期実行する方が単純。

例:

```bash
intel collect
intel analyze
intel digest
intel run
intel doctor
```

macOSならlaunchd、Linuxならsystemd timerやcronを想定。PCが停止していた場合でも再開後にカーソルから追いつけるよう、収集処理は冪等にする。

---

## 4. 推奨アーキテクチャ

全体像:

```text
情報源
  ↓
収集・正規化
  ↓
重複排除・本文抽出
  ↓
事前選別
  ↓
LLMによる構造化分析
  ↓
ランキング・日次ダイジェスト
  ↓
Obsidian Markdown / 将来のWeb UI
```

重要なのは、処理段階を分離しつつ、初期実装は単一プロセスで済ませること。

---

## 5. 推奨技術スタック

### 5.1 言語・主要ライブラリ

推奨はPython。

候補:

- Typer: CLI
- Pydantic: データモデル・バリデーション
- httpx: HTTP/API取得
- feedparser: RSS/Atom
- trafilatura等: 記事本文抽出
- SQLAlchemy + Alembic: DBアクセス・マイグレーション
- OpenAI / Anthropic等の公式SDK: LLM分析
- pytest: テスト
- ruff / mypy: 静的チェック

理由:

- 収集、本文抽出、バッチ処理、LLM連携を一言語で扱いやすい
- 個人用ツールではPythonの開発速度が高い
- TypeScriptはObsidianプラグインが必要になった段階で追加すればよい

### 5.2 データベース

初期版はSQLiteを推奨。

理由:

- 個人用・単一ホストなら十分
- 運用が軽い
- バックアップしやすい
- FTSで全文検索もできる
- PostgreSQLやベクトルDBは初期には過剰

---

## 6. 最低限のデータモデル案

```text
sources
  id
  type
  name
  config
  cursor
  last_success_at
  last_error_at
  enabled

items
  id
  source_id
  external_id
  url
  canonical_url
  title
  author
  published_at
  fetched_at
  raw_payload
  content_hash
  status

documents
  id
  item_id
  extracted_text
  language
  extraction_status
  extraction_error

analyses
  id
  item_id
  summary
  topics
  entities
  relevance_score
  novelty_score
  actionability_score
  recommendation_reason
  possible_actions
  uncertainties
  model
  prompt_version
  input_hash
  analyzed_at
  estimated_cost

signals
  id
  item_id
  viewed
  saved
  dismissed
  rating
  user_note
  created_at

digests
  id
  period
  generated_at
  markdown
  item_ids
```

重要:

- ObsidianをDBにしない
- 収集状態、カーソル、分析履歴、プロンプトバージョン、コスト、重複情報はSQLiteで管理する
- Obsidianは閲覧・編集・人間向け出力先と考える

---

## 7. Obsidian連携方針

### 7.1 結論

初期版はAPI連携ではなく、VaultへのMarkdown直接出力でよい。

Obsidian Vaultは通常のフォルダとMarkdownファイル群として扱えるため、サービスからファイルを書き出すだけで実用上の連携は成立する。

### 7.2 連携方法の優先順位

1. Markdown直接出力  
   - 初期版の推奨
   - 最も単純
   - 障害点が少ない

2. Obsidian固有機能の利用  
   - 公式Plugin APIやURIスキーム等を検討
   - 実装前に最新の公式ドキュメントで確認する

3. Obsidian内UIが欲しい場合  
   - TypeScriptでObsidian Pluginを作る
   - 「重要」「不要」「既知」ボタン、未処理件数、再分析要求などを出す場合に有効

4. REST API的な操作  
   - コミュニティ製Local REST APIプラグイン等が候補
   - 公式APIではない可能性があるため、採用前に保守状況・セキュリティ・Obsidian起動要件を確認する
   - インターネット公開は避け、localhostまたはVPN/Tailscale等に限定すべき

5. サーバー上のVault同期  
   - Obsidian SyncやHeadless系の仕組みが候補になり得る
   - ただし、実装前に現在の公式対応状況を必ず確認する

### 7.3 出力先例

```text
Vault/
└── Intelligence/
    ├── Daily/
    │   └── 2026-07-05.md
    ├── Items/
    │   └── 2026-07-05-example-title.md
    └── Topics/
        ├── AI.md
        └── Business.md
```

### 7.4 Daily DigestのMarkdown例

```markdown
# Intelligence Digest — 2026-07-05

## 最優先

### 1. 記事タイトル
- 要約:
- なぜ重要か:
- 既知情報との差分:
- 判断・行動への示唆:
- 不確実な点:
- 出典:

## 継続監視

## 反対意見・異なる見方

## 後で読む
```

### 7.5 自動生成ノートの安全な扱い

各ノートには固定IDとメタ情報を持たせる。

```yaml
---
intel_id: "news_01JXYZ..."
source_type: "rss"
source_url: "https://example.com/article"
generated_at: 2026-07-05T08:00:00+09:00
status: unread
managed_by: intelligence-service
---
```

自動生成領域と手動編集領域を分ける。

```markdown
<!-- GENERATED:START -->
ここはサービスが上書きしてよい領域
<!-- GENERATED:END -->

## My Notes

ここは人間が自由に編集する領域
```

---

## 8. LLM分析方針

### 8.1 初期版ではエージェント化しない

LangChain等の大きなエージェントフレームワークは初期版では不要。

まずは、各アイテムを構造化出力で分析するだけで十分。

### 8.2 分析JSONの例

```json
{
  "summary": "string",
  "topics": ["string"],
  "entities": ["string"],
  "relevance_score": 0,
  "novelty_score": 0,
  "actionability_score": 0,
  "recommendation_reason": "string",
  "possible_actions": ["string"],
  "uncertainties": ["string"]
}
```

### 8.3 必ず保存するメタ情報

- 使用モデル
- プロンプトバージョン
- 入力本文のハッシュ
- 分析日時
- 推定コスト
- エラー内容
- 再分析が必要かどうか

### 8.4 プロンプト設計で重視する観点

単なる要約ではなく、以下を出させる。

- 何が新しいのか
- ユーザーにとってなぜ重要か
- 既知情報との差分は何か
- 判断や行動にどう影響するか
- 読まなくてよい可能性はあるか
- 反対意見や別解釈はあるか
- 不確実な点は何か

---

## 9. ランキング・推薦の考え方

初期KPIは「たくさん集められること」ではない。

推奨KPI:

> 毎日最大10件のダイジェストを生成し、そのうち最低3件は実際に読む価値がある。

ランキングに使う候補スコア:

- relevance_score: ユーザー関心との関連度
- novelty_score: 新規性
- actionability_score: 行動へのつながり
- source_weight: ソース信頼度または優先度
- recency_score: 鮮度
- duplication_penalty: 同一ニュース重複の減点
- user_feedback_boost: 過去の保存・既知・不要フィードバック

避けるべき失敗:

> 未読記事を未読要約に変えるだけになること。

要約量より、読むべきものを減らすことが価値。

---

## 10. 初期スコープ

### 10.1 情報源は3種類まで

初期版では以下に絞る。

1. RSSまたはニュースサイト
2. SNSを1種類
3. 任意URLの手動投入

複数SNSへの同時対応は後回し。コネクター実装だけで開発が分散する。

### 10.2 最初の成果物

> 毎日1回、Obsidianに最大10件のダイジェストMarkdownを生成する。

これをMVPの中心に置く。

### 10.3 最初に作るCLI

```bash
intel collect      # 情報源から取得
intel extract      # 本文抽出
intel analyze      # LLM分析
intel digest       # ランキングとMarkdown生成
intel export       # Obsidianへ出力
intel run          # 一括実行
intel doctor       # 設定・APIキー・DB・Vaultパス確認
```

---

## 11. 1週間の実装案

### Day 1

- リポジトリ作成
- Pythonプロジェクト雛形
- SQLiteスキーマ
- CLI雛形
- RSSコネクター

### Day 2

- URL正規化
- 重複排除
- 本文抽出
- 取得カーソル管理

### Day 3

- LLM構造化分析
- プロンプトバージョン管理
- 分析結果保存

### Day 4

- ランキングロジック
- Daily Digest生成
- Obsidian Markdown出力

### Day 5

- SNSコネクター1種類
- エラー処理
- リトライ
- `intel doctor`

### Day 6-7

- 実運用
- 保存／不要／既知／重要のフィードバック記録
- ダイジェストの品質確認
- ローカル運用で問題が起きるか計測

---

## 12. 推奨リポジトリ構成

```text
personal-intelligence/
├── app/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── connectors/
│   │   ├── base.py
│   │   ├── rss.py
│   │   ├── manual.py
│   │   └── sns_xxx.py
│   ├── extraction/
│   │   ├── article.py
│   │   └── normalize.py
│   ├── analysis/
│   │   ├── llm.py
│   │   ├── prompts.py
│   │   └── schema.py
│   ├── ranking/
│   │   └── ranker.py
│   ├── export/
│   │   ├── obsidian.py
│   │   └── markdown.py
│   └── digest/
│       └── daily.py
├── migrations/
├── prompts/
│   └── item_analysis_v1.md
├── tests/
├── data/
│   └── .gitkeep
├── Dockerfile
├── pyproject.toml
├── README.md
└── .env.example
```

---

## 13. セキュリティ・法務・運用上の注意

### 13.1 外部コンテンツは未信頼データ

ニュース記事やSNS投稿には、プロンプトインジェクションに相当する文言が含まれる可能性がある。

対策:

- 外部本文を命令として扱わない
- LLMにはファイル削除、投稿、外部送信などの権限を与えない
- 構造化出力をスキーマ検証する
- 自動実行アクションは初期版では入れない

### 13.2 API・利用規約

SNSやニュースサイトはAPI制約・利用規約が重要。

避けるべき設計:

- APIが面倒だから無断スクレイピングする
- rate limitを無視する
- 取得可能期間を把握せず、後からバックフィルできると思い込む
- トークンを平文で雑に管理する

### 13.3 全文保存の扱い

ニュース記事やSNS投稿を無期限に全文保存する設計は避けた方がよい。

保存するものの基本方針:

- URL
- タイトル
- 著者
- 公開日時
- 短い抜粋または一時キャッシュ
- 自分で生成した要約・評価
- 自分のメモ

全文キャッシュには保持期限を設ける。

---

## 14. 厳しめの設計指摘

現状の構想をそのまま進めると、以下の失敗パターンになりやすい。

1. 多くの情報を取得できる
2. きれいに要約できる
3. Obsidianに大量のノートが増える
4. 数週間後に読まなくなる

避けている本質的な問い:

> このサービスによって、どの判断または行動を、今より良くしたいのか。

したがって、MVPでは「情報を集める量」ではなく、「読むべきものを選ぶ精度」を検証する。

最初から目標を以下に固定するのがよい。

> 今日見るべき5〜10件を選び、それぞれについて「なぜ重要か」「自分に何を示唆するか」を説明する。

---

## 15. 未決事項・次に確認すべきこと

別エージェントが次に詰めるべき論点。

### 15.1 情報源

- 初期対象SNSは何か
- RSS対象はどのサイトか
- ニュースソースは日本語中心か英語中心か
- 手動URL投入の方法はCLIか、ブラウザ拡張か、Obsidian内リンクか

### 15.2 評価基準

- ユーザーにとって「読む価値がある」とは何か
- 関心領域は何か
- 事業、投資、技術、キャリア、趣味などの優先順位はどうするか
- 既知情報との差分をどう判定するか

### 15.3 出力

- Daily Digestは何時に生成するか
- 1日何件までにするか
- Obsidian内の出力フォルダはどこか
- 自動生成ノートを手動編集するか
- モバイル閲覧を重視するか

### 15.4 運用

- ローカルPCは毎日どの程度起動しているか
- APIキーやSNSトークンをどこに置くか
- バックアップをどうするか
- 失敗通知は必要か
- コスト上限をいくらにするか

### 15.5 実装前の最新確認

以下は技術仕様やサービス状況が変わる可能性があるため、実装直前に公式情報で確認する。

- Obsidianの公式API / Plugin API / URI / Sync / Headless相当機能の現在状況
- 対象SNSのAPI料金・rate limit・取得可能期間
- LLMプロバイダーの構造化出力仕様と料金
- 記事本文抽出ライブラリの保守状況

---

## 16. 次アクションの優先順位

1. MVPの成功条件を確定する  
   - 例: 「毎日最大10件、最低3件は読む価値がある」

2. 初期情報源を3つに絞る  
   - RSS/ニュース
   - SNS 1種類
   - 手動URL

3. Obsidian出力形式を決める  
   - Daily Digest
   - Item Note
   - Topic Noteは後回しでも可

4. SQLiteスキーマとCLIを作る

5. RSSだけで一通り流す

6. LLM分析とランキングを入れる

7. 7日間運用し、ローカル運用で問題があるか確認する

8. 必要が出たらサーバー化またはObsidianプラグイン化を検討する

---

## 17. エージェントへの注意

このプロジェクトでは、ユーザーに対して単に「よさそう」と肯定するのではなく、以下を厳しく見ること。

- 情報量を増やすだけの設計になっていないか
- 要約を増やして未読負債を作っていないか
- API連携やプラグイン開発に過剰投資していないか
- 実際の意思決定・行動改善につながっているか
- 初期実装が大きすぎないか
- ローカル/サーバーの選択を価値検証前に複雑化していないか

基本方針:

> 小さく作り、毎日のダイジェストの有用性で検証し、必要になった部分だけ拡張する。
