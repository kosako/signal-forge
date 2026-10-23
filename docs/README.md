# 文書の索引

signal-forge の文書の一覧と、それぞれの位置づけをまとめる。リポジトリ全体の入口は [../README.md](../README.md)。

## 正本の優先順位

| 対象 | 正本 | 備考 |
|---|---|---|
| 企画(何を・なぜ作るか、KPI、スコープ、撤退条件、フェーズ 2 境界) | [PROPOSAL.md](PROPOSAL.md) | repo 外のナレッジツールにある企画書の写し。企画を変えるときは repo 外の正本を直してから写す。設計・計画と食い違えば PROPOSAL が優先する |
| 技術設計 | [design/](design/) 00〜06 | 各文書の冒頭で PROPOSAL を上位文書としている。朝バッチの段の順序は設計 00 §2、リトライとタイムアウトの数値は設計 05 §8 が正本 |
| MVP の実行計画の定義 | [project/plan.md](project/plan.md) | マイルストーンの完了条件・Issue の定義・リスク台帳。設計は design を正本として参照する。対象は MVP(M0〜M3)で、フェーズ 2(深掘り)は設計 06 と ADR 0002〜0005 にある |
| 判断の記録 | [adr/](adr/) | 判断した時点の記録。下の「ADR」の節を参照 |
| 進捗 | GitHub Issues と repo 外のナレッジツール | repo の文書には進捗を書かない(plan.md のチェックボックスも定義の一部で、進捗としては更新しない) |

PROPOSAL.md と project/plan.md が参照先として挙げる `.agent-context.local.md` は、repo に含めない非公開のローカルメモで、repo には無い。

## 文書の種別

- **仕様**: 上書きで最新を保つ。食い違いを見つけたら文書側を直す。
- **記録**: 当時のまま残す。本文は書き換えない(冒頭への注記の追加は除く)。現行の内容は仕様の文書を見る。
- **雛形**: コピーして使う。記入した結果は repo に置かない。
- **道具**: 実行して使うもの(SQL・スクリプト)。

## repo に置くもの・置かないもの

このリポジトリは公開して読まれる前提で書く(GH#163)。

- **repo に置く**: コードと同じ PR で直してレビューするもの。設計・ADR・runbook・プロンプト・KPI の SQL・雛形。
- **repo の外(非公開のナレッジツール)に置く**: 作業ログと進捗(上の「正本の優先順位」のとおり)、予算・残高・利用件数などの実数、仕事や暮らしに触れる例、検討して捨てた案の細かな経緯。
- secret・ローカルのパス・アカウントの ID は、文書に限らず repo に入れない。
- この決まりはこれから書く分に当てる。既にある記録は当時のまま残す。

## 一覧

### 企画と用語

| 文書 | 説明 | 種別 |
|---|---|---|
| [PROPOSAL.md](PROPOSAL.md) | 企画書(確定版 v1.1)。何を・なぜ作るか、KPI の測定定義、スコープ、安全条件、撤退・ピボット条件、フェーズ 2 境界 | 仕様(repo 外の正本の写し) |
| [../CONTEXT.md](../CONTEXT.md) | フェーズ 2(retrieval・深掘り・知見)の用語集。語の定義だけを持つ。MVP の語(items・analyses・signals など)は設計 01・03 を見る | 仕様 |

### 技術設計(design/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [design/00-architecture.md](design/00-architecture.md) | 全体アーキテクチャ。2 プロセス構成(朝バッチ `intel run` / 受信デーモン `intel poll`)、朝バッチの段の順序(§2 が正本)、モジュール構成と依存方向、設定・secrets、LLM 抽象層と隔離、エラー方針 | 仕様 |
| [design/01-data-model.md](design/01-data-model.md) | データモデル。SQLite のテーブル、URL の正規化と重複排除、本文の保持期限と削除、signals、migration の運用 | 仕様 |
| [design/02-connectors.md](design/02-connectors.md) | 情報源のコネクタ(X・RSS・手動 URL)と `intel collect` の取り込み契約(カーソル、エラー分類、連続失敗の通知) | 仕様 |
| [design/03-analysis-ranking.md](design/03-analysis-ranking.md) | 分析とランキング。事前選別、LLM のプロンプトと出力スキーマ、関心プロファイルの形式、スコア式、コスト上限、プロンプトインジェクション対策 | 仕様 |
| [design/04-delivery-telegram-obsidian.md](design/04-delivery-telegram-obsidian.md) | 出口層。Telegram の配信と受信(メッセージ形式、ボタン、リプライの振り分け、認可)と Obsidian へのノート出力の規則 | 仕様 |
| [design/05-ops-runtime.md](design/05-ops-runtime.md) | 運用設計。launchd の構成、`intel doctor` の検査項目、ログ、失敗通知、バックアップ、secrets、月次コスト、リトライとタイムアウトの規約(§8 が単一正本)、開発フロー | 仕様 |
| [design/06-deepdive-experience.md](design/06-deepdive-experience.md) | フェーズ 2 の深掘り体験。夜のリプライで指定 → 朝の調査段 → 概要カード → カードへの質問への返事と take。ADR 0002〜0005 を具体化する | 仕様 |

### 判断(adr/)

ADR の一覧は下の「ADR」の節にある。

### 計画と週次レビュー(project/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [project/plan.md](project/plan.md) | MVP(M0〜M3)の実行計画の定義。完了条件、Issue バックログ、進行ルール、リスク台帳、週次レビューの型、興味ループの段階化 | 仕様(MVP の範囲) |
| [project/kpi-queries.md](project/kpi-queries.md) | 週次レビューで使う KPI 集計クエリ([../scripts/kpi/](../scripts/kpi/))の索引と実行方法 | 仕様 |
| [project/weekly-review-template.md](project/weekly-review-template.md) | 週次レビューの記入テンプレート。結果は repo 外のナレッジツールに書く | 雛形 |

### 運用(ops/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [../ops/runbook.md](../ops/runbook.md) | 運用手順書(手を動かす手順だけ。理由は設計 05)。§1〜§5 が launchd のセットアップ、§6 配信時刻の変更、§7 バックアップと復旧、§8 secrets のローテーション、§9〜§10 X の連携、§11 カードへの質問への返事、§12 ソースの見直し、§13 深掘りノートのレビュー、§14 Codex が失敗したときの予備 | 仕様 |
| [../ops/launchd/com.intel.morning.plist](../ops/launchd/com.intel.morning.plist) | 朝バッチ(`intel run`)の LaunchAgent の雛形。プレースホルダを runbook §1 で置換して配置する | 雛形 |
| [../ops/launchd/com.intel.poller.plist](../ops/launchd/com.intel.poller.plist) | 受信デーモン(`intel poll`)の LaunchAgent の雛形。プレースホルダを runbook §1 で置換して配置する | 雛形 |

### 雛形(templates/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [templates/interest-profile.md](templates/interest-profile.md) | 関心プロファイルの雛形。`~/.config/intel/interest-profile.md` にコピーして使う | 雛形 |

### 道具(scripts/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [../scripts/kpi/](../scripts/kpi/) | 週次レビュー用の KPI 集計 SQL(01〜08)。使い方は [project/kpi-queries.md](project/kpi-queries.md) | 道具 |
| [../scripts/day0_x_probe.py](../scripts/day0_x_probe.py) | Day 0 ゲートで X API を実測した使い捨てのスクリプト。本体からは使わない | 記録 |

### 企画段階の記録(plans/)

企画を比べて決めるまでの記録。確定版は [PROPOSAL.md](PROPOSAL.md) で、食い違えば PROPOSAL が優先する。

| 文書 | 説明 | 種別 |
|---|---|---|
| [plans/00-comparison.md](plans/00-comparison.md) | 企画の 3 パターン(A/B/C)の比較と推奨(A-lite 直行)の記録(v3) | 記録 |
| [plans/plan-a-minimal-scratch.md](plans/plan-a-minimal-scratch.md) | パターン A(最小フルスクラッチ。採用案の元)の企画書ドラフト v1 | 記録 |
| [plans/plan-b-full-loop.md](plans/plan-b-full-loop.md) | パターン B(深掘りループまで含む完成形)の企画書ドラフト v1。現行の深掘りの設計は設計 06 | 記録 |
| [plans/plan-c-hybrid.md](plans/plan-c-hybrid.md) | パターン C(既製品ハイブリッド)の企画書ドラフト。不採用 | 記録 |
| [plans/review-codex-20260705.md](plans/review-codex-20260705.md) | 企画書ドラフト v1 への Codex レビューの指摘と対応(2026-07-05) | 記録 |
| [plans/intelligence_service_handoff.md](plans/intelligence_service_handoff.md) | 2026-07-05 時点の初期構想の引き継ぎメモ。現行の仕様でも、エージェントへの現在の指示でもない | 記録 |

### 設計段階の記録(reviews/)

| 文書 | 説明 | 種別 |
|---|---|---|
| [reviews/2026-07-05-codex-design-review.md](reviews/2026-07-05-codex-design-review.md) | PROPOSAL・設計 00〜05・plan v1 への Codex 設計レビューの指摘と反映内容(2026-07-05) | 記録 |

レビューの記録は段階で置き場所を分けている。plans/ は企画段階、reviews/ は設計段階。

## ADR

ADR(Architecture Decision Record)は、覆しにくい判断とその理由・却下した案の記録。

| # | 題 | 状態 | 日付 | 1 行 | 関係 |
|---|---|---|---|---|---|
| [0001](adr/0001-interest-feedback-loop.md) | 興味フィードバック・ループと X ソース戦略 | Accepted | 2026-07-10 | 手動のアカウントリストをやめ、ホーム TL・検索・ブックマーク/いいね/自分の RT などの行動を燃料に、予算内で段階的に回す | 入力側の判断。0002 が参照する |
| [0002](adr/0002-retrieval-agent-via-mcp.md) | retrieval の主経路は「エージェント経由(MCP)」とする | Accepted | 2026-07-12 | 貯めた知見は、人の検索窓ではなく作業中の AI エージェントが MCP 経由で引く | MCP の口は未実装 |
| [0003](adr/0003-knowledge-item-as-deepdive-byproduct.md) | 知見の単位は「深掘りの副産物」 | Accepted | 2026-07-12 | 知見 = ソース + 出典群 + ユーザーの結論(take)。AI がドラフトし、take が付いたものだけを確定とする | 0002 を前提とする。take の記録は GH#106 で実装(解説ノートへの追記と followup_dig) |
| [0004](adr/0004-deepdive-in-agent-obsidian-substrate.md) | 深掘りは既存エージェント内 / 知見の substrate は Obsidian | Accepted | 2026-07-12 | 深掘りは既存のエージェントの中で行い、知見はダイジェストと同じ Obsidian vault の別フォルダに貯める | 0002・0003 を前提とする。「専用 UI は延期」の部分は 0005 で置き換え |
| [0005](adr/0005-operation-surface-separate-app.md) | 機能は signal-forge が口として持ち、操作するアプリは別プロジェクトで作る | Accepted | 2026-10-18 | 操作面を差し替えられる形に寄せる。当面の操作面は Telegram で、カードへの質問への返事を先に作る | 0004 の一部を置き換える。中央アプリへ出す口(0002 の MCP の方向)は口を出すときに改めて決める |

ADR の扱い:

- Accepted になった ADR の本文は書き換えない。判断を変えるときは新しい ADR を立てる。
- 例外として、置き換えられたときにステータス行へ置き換えの注記を追記することだけはしてよい(0004 のステータス行が実例)。

## 文書を足すときの決まり

- 設計文書は番号を続ける(`design/07-<名前>.md`)。冒頭のステータス行に上位文書(PROPOSAL)を書く。
- ADR は連番で `adr/NNNN-<名前>.md` を足し、冒頭にステータス(Accepted と日付)と関連を書く。この索引の「ADR」の表に行を足す。
- どの文書を足したときも、この索引の一覧に行を足す。
