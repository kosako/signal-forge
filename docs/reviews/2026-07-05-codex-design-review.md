# レビュー記録: Codex 設計レビュー反映(2026-07-05)

- **レビュー対象**: `docs/PROPOSAL.md`(企画正本 v1)、`docs/design/00〜05`(設計6文書)、`docs/project/plan.md`(実行計画 v1)
- **レビュアー**: Codex(指摘11件: must 4 / should 6 / nit 1)
- **反映者**: Claude
- **反映方針**: PROPOSAL が正本。設計・計画との乖離は原則として設計・計画側を PROPOSAL に合わせて修正。PROPOSAL 側の変更は、指摘が PROPOSAL 自体の欠陥(ロードマップのクリティカルパス、MVP スコープの測定不能性)を突いた 2 点のみ(→ v1.1)。

## 指摘と対応(全11件、全件反映・見送りなし)

### must

| # | 指摘(要約) | 対応 |
|---|---|---|
| 1 | X 不合格時の「RSS+手動URLのみ」縮退ルートで doctor の X 疎通チェックが恒久 FAIL になる | **反映**。05 §2 #5: X 無効時(`x.enabled=false` / sources の type='x' 全行 enabled=0)は SKIP/WARN。plan.md: M2a 完了条件を「X あり」/「RSS+手動URLのみ」で分岐、#21 に SKIP 実装の完了条件を追加 |
| 2 | API LLM 利用時、$30 キャップを実行前に止める場所がない(budget check は collect 直前のみ、daily_llm_limit は件数でドル上限でない) | **反映**。03 §7: 執行点を2箇所に(collect 直前+ analyze 直前の API 課金エンジン用予算判定。Week 1 実測トークンから「1日最大 LLM 費」を見積り、超過見込みは daily_llm_limit 縮小 or タイトルのみ degraded)。plan.md #20: 依存に #9/#11/#14 を追加、完了条件に analyze 前判定を追加 |
| 3 | 手動 URL が空 content_hash の 24h dedup で別 URL まで duplicate 化されうる | **反映**。01 §5/§6: 正規化テキストが空または20字未満のアイテムは content_hash dedup の対象外、manual の挿入直後の重複判定は canonical_url のみ。02 §1/§4・04 §5 の content_hash 照会記述も同旨に修正。plan.md #18: 「同日に別URLを2件投入して両方 new」テストを完了条件に追加 |
| 4 | digest_messages のヘッダ行(item_id NULL)が SQLite の UNIQUE で一意にならず部分再送の冪等性が崩れる | **反映**。01 §3: 部分 UNIQUE index 2本(`WHERE item_id IS NOT NULL` / `IS NULL`)で担保。message_kind 列案より、列と挿入コードの変更が不要な index 2本を採用(理由を DDL コメントに1行明記) |

### should

| # | 指摘(要約) | 対応 |
|---|---|---|
| 5 | フェーズ2境界「🔖の50%以上を深掘り」が MVP で測れない(PROPOSAL は追い掛けで記録すると規定、04 は MVP スコープ外、#24 に followup_dig クエリなし) | **反映(PROPOSAL を正とし下位を修正)**。04 §4: 「MVP スコープ外」を撤回し、🔖翌日夜の追い掛け1通(「深掘りした? はい/いいえ」)を MVP の最小形として規定。01 §7 の「スキーマのみ」も送信込みに修正。plan.md #19: 追い掛け送信+記録を完了条件に追加(Week 2 に収まらない場合の格下げ先: 手動リマインダ+手動 SQL 記録を1行明記)、#24: 🔖累計+深掘り実施率の SQL を追加。PROPOSAL: やること/やらないことに MVP スコープを明記(v1.1) |
| 6 | intel run の deliver/export 順序が文書間で割れ、「Telegram 失敗でも export 完了」と噛み合わない | **反映**。正順序を collect→extract→analyze→digest→export→deliver→purge に一本化(00 §2 を正本と明記)。deliver 失敗時は export 済みのまま `delivery_status='failed'`。00 §1 図・00 §6・05 §1.1・plan.md #14/#16 を統一 |
| 7 | 「無効日3日連続なら配信5件に削減」の実装ノブがない | **反映**。config に `digest.max_items`(既定10)を追加(03 §6・00 §4)。plan.md #12(config 化)/#24(検知クエリ+変更手順)・R1・§6 に反映 |
| 8 | RSS の raw_content の永続先が未定義(collect→extraction は DB 経由なのに受け渡し場所がない) | **反映**。01 §6: `items.raw_payload` の JSON 形(`raw_content`/`meta`/`x`)を定義。サイズ上限(raw_content 50,000字で切り詰め)と purge 対象(ソース別 TTL で NULL 化。従来の X 限定 NULL 化を全ソースに一般化)を明記。00 §3・02 §1(RawItem)と整合 |
| 9 | Day 0 の「リスト方式再検討」がタスク化されていない | **反映**。plan.md #1: 「ホームTL継続 / 厳選リスト / 追跡専用アカウントのいずれかの決定記録」を完了条件に追加。02 §2 Day 0 テスト仕様: リスト取得可否・実効単価の小確認行を追加 |
| 10 | M2(3〜5日)に運用ハードニングが詰まりすぎ、計測開始のクリティカルパスが太い | **反映**。plan.md: M2 を M2a「計測開始可能」(#15〜#19・#21・#22)と M2b「運用ハードニング」(#20・#23・#25。#25 は M3 から移動)に分割し、M3 は M2a 到達で開始・M2b は M3 と並走可と明記。PROPOSAL ロードマップにも M2a/M2b を反映(v1.1) |

### nit

| # | 指摘(要約) | 対応 |
|---|---|---|
| 11 | llm.engine の値名が codex / codex-cli で揺れ | **反映**。`codex-cli` / `claude-cli` / `anthropic-api` に統一(00 §5 の config 例を修正。01 の analyses.engine enum と plan.md #3 は既に同語彙で変更なし) |

## 修正ファイル一覧

- `docs/PROPOSAL.md`(v1 → v1.1: ステータス行・経緯4項・スコープ2箇所・ロードマップ M2a/M2b)
- `docs/design/00-architecture.md`(#6 順序正本化・#7 config・#8 DB 経由明記・#11 engine 値名)
- `docs/design/01-data-model.md`(#3 dedup スキップ・#4 部分 UNIQUE・#5 followup_dig・#8 raw_payload)
- `docs/design/02-connectors.md`(#3 canonical_url 照会・#8 raw_content 永続先・#9 リスト小確認)
- `docs/design/03-analysis-ranking.md`(#2 analyze 前予算判定・#7 digest.max_items)
- `docs/design/04-delivery-telegram-obsidian.md`(#3 URL 照会・#5 追い掛け MVP 化)
- `docs/design/05-ops-runtime.md`(#1 doctor X SKIP・#6 intel run 順序)
- `docs/project/plan.md`(v1 → v1.1: M2a/M2b 分割、#1/#12/#14/#16/#18/#19/#20/#21/#23/#24 の完了条件・依存、依存図、リスク台帳・週次レビュー)
