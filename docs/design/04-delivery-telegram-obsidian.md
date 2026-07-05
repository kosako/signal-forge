# 設計 04: 出口層 — Telegram 配信 / Obsidian 蓄積

ステータス: Draft v1(2026-07-05)/ 正本の企画: `docs/PROPOSAL.md`

対象コード: `app/delivery/telegram.py`(送信)、受信デーモン(`intel poll`。CLI 名の正本は設計 00 §3 / 05 §1.2)、`app/export/obsidian.py`。入力は `digests`(生成済み Markdown と item_ids)と `items` / `analyses`。出力は `signals` への記録と Vault へのファイル書き込み。

## 1. Telegram 全体方針

- Bot API は **httpx 直叩き**(共通規約)。接続は **long polling 固定**、`getUpdates` の offset は SQLite の専用テーブル(`telegram_state`。DDL は 01 §3)に永続化する — デーモン再起動で取り漏らし・二重処理しないため。
- parse_mode は **HTML** を採用 — MarkdownV2 はエスケープ対象が多く事故りやすい。エスケープ規定: **テキストノードは `& < >` の3文字、属性値(`href` 等)はさらに `"` と `'` も**エスケープする(canonical_url は外部制御可能であり、`"` 入り URL による属性破壊を防ぐ)。
- 本文由来の文字列(タイトル・要約・理由)は**必ず HTML エスケープ**して埋め込む。URL は LLM 出力でなく **items テーブルの正規 URL のみ**(PROPOSAL のインジェクション対策)。scheme は 01 §4 の allowlist(http/https のみ)で挿入時に保証済み。

## 2. 朝ダイジェストのメッセージフォーマット

構成は **ヘッダ1通+アイテム最大 `digest.max_items` 通(既定10。設計 03 §6)**(1アイテム=1メッセージ。ボタンをアイテム単位に付けるため)。

- ヘッダ: `📡 2026-07-05(土)のダイジェスト — 8件` + degraded 時は `⚠️ 本文抽出失敗/分析縮退あり` を付記。
- アイテム1通のテンプレート(上から順に):

```
{順位}. <b>{タイトル}</b>
<a href="{正規URL}">{ソース名}</a>{published_at があれば ・M/d}
{要約 2〜3文}
💡 {自分への示唆 1文}
```

- **長さ制限**: Telegram は1通4096字だが、安全側で **1通1000字(概算)を上限**にし、超過分は要約末尾を `…` で切り詰める(タイトル・URL・示唆は削らない)— 制限は要約で吸収するのが読みやすさを壊さない。
- 各アイテムに InlineKeyboard 1行4ボタン: `👍 価値あり / 🔖 深掘り / ✅ 既知 / 👎 不要`。
- 文体例(1件分、日本語ダイジェストのトーンの正):

> 2\. **Anthropic、企業向け Claude に監査ログ API を追加**
> [TechCrunch ・7/4]
> エンタープライズ管理者が全メンバーの利用ログをエクスポートできる API が公開された。SOC2 対応を理由に導入を渋っていた企業の障壁が一つ減る。料金プランは Enterprise のみ。
> 💡 顧客先で「ログが取れないから NG」と言われた件の再提案材料になる。

- **配信の冪等性**: 送信完了時に `digests.delivery_status='sent'` + `sent_at` を記録し(語彙は 01 §3 に統一。delivered_at という別名は使わない)、`intel deliver` は既配信の digest を再送しない(再実行安全)。送信途中失敗時は未送信アイテムのみ再送できるよう、送信済み message_id をアイテムごとに `digest_messages`(01 §3)へ記録する。

## 3. callback_query 処理(受信デーモン)

- **callback_data 形式**(64バイト制限内・ASCII のみ):
  - 反応ボタン: `s:{item_id}:{up|dig|knw|no}`(👍🔖✅👎に対応)
  - 夜アンケート: `q1:{YYYYMMDD}:{y|n}` / `q2:{YYYYMMDD}:{分}` / 深掘り追い掛け: `f:{item_id}:{y|n}`
- 処理順は **①即 answerCallbackQuery(10秒失効対策)→ ② signals に INSERT → ③ editMessageReplyMarkup で選択状態を反映**(選ばれたボタンに `✓` を付けた盤面に差し替え)。
- **INSERT 前の検証**: パースした item_id が items に実在し、かつ**いずれかの `digests.item_ids` に含まれる**ことを確認してから signals に書く(signal_date の帰属=その digest_date もこの照合で同時に解決)。不一致・不正形式の callback_data は記帳せずログのみ(古いメッセージや偽造データで KPI 分母外に記帳されるのを防ぐ)。
- **押し直し許容**: signals は追記型(append-only)で記録し、KPI 集計時は item ごとの最新レコードを採用 — 誤タップを訂正可能にしつつ履歴を失わない。
- answerCallbackQuery の text は短く(例: `👍 記録しました`)。DB 書込失敗時のみ `⚠️ 記録失敗、もう一度` を返す。

## 4. 夜アンケートと 🔖 追い掛け

- 送信ロジックは `delivery/telegram.py` の関数として実装するが、**起動は受信デーモン(`intel poll`)の内蔵タイマー(21:30 JST 仮、config で変更可)**とする(設計 05 §1.2 と同一方式。plist を2枚に抑え、朝バッチ以外の Telegram 送信経路をポーラーに集約する。別 launchd ジョブ+`--survey` 方式は採らない)。
- 質問1「今日、情報収集目的の巡回をした?」(はい/いいえ)、質問2「読了時間」はボタン `5/10/15/20/30分` の5択 — 自由入力より1タップ原則を優先。
- 翌日 12:00 JST までの遅延回答は callback_data 内の日付で正しい日に記帳される(送信日時に依存しない)。未回答の fail-closed 集計は手動集計側の責務(本層は記録のみ)。
- **🔖 追い掛け送信は MVP に含める(最小形)**: 夜アンケート送信の直後に、**前日 🔖 が付き `followup_dig` 未記録のアイテムごとに「深掘りした?(はい/いいえ)」を1通ずつ送る**(callback_data `f:{item_id}:{y|n}`、記録は 01 §7 の `followup_dig`)。理由: フェーズ2境界の進出条件「🔖の50%以上を実際に深掘り」(PROPOSAL §フェーズ2境界が正)は追い掛け記録がないと MVP 期間中に測定できない。※旧記載「MVP スコープ外」は Codex レビュー(2026-07-05)で PROPOSAL 側に合わせて修正。
- 実装が Week 2 に収まらない場合の格下げ先: 送信を手動(自分宛ての固定リマインダ)にし、回答を手動 SQL で `followup_dig` に記録して判定材料とする(1行運用)。

## 5. URL 投入受付

- デーモンは owner からの**平文メッセージ中の http/https URL をすべて抽出**し、`sources`(type=manual)経由で `items` に投入。既存 URL(**canonical_url 一致のみ。**content_hash は使わない — manual は本文が無く空ハッシュになるため。01 §5〜6 / 02 §4)は再投入せず「投入済み」と返信。
- 応答例: `✅ 1件受け付けました。明朝のダイジェストで分析します`。URL を含まないメッセージには `URL を送ると翌朝のダイジェスト候補になります` と定型応答(コマンド体系は作らない — MVP の操作は URL 投入だけで足りる)。

## 6. 認可

- config.toml の `[telegram] owner_user_id` / `owner_chat_id` を **allowlist の正**とする。message は `from.id`、callback_query も `from.id` で検証(chat_id だけではグループ転送経由をすり抜けるため)。
- 不一致の update は **poller.log への WARN 1行(JSON Lines: `from.id`・update 種別・受信時刻のみ。本文ペイロードは保存しない)で記録して破棄**(応答も返さない — bot の存在を確認させない)。第三者の URL 注入と KPI 汚染の遮断が目的。専用 DB テーブル(rejected_updates)は作らない — 1人運用で照会頻度がほぼゼロのデータに untrusted ペイロードの無制限蓄積面を作らない。頻発が観測されたら初めてテーブル化を検討する。

## 7. Obsidian 出力

- **設定**: config.toml の `[obsidian] vault_path` + `folder`(既定 `SignalForge`)。パスはハードコード禁止。書込は **一時ファイル→rename のアトミック書き**(同期ツールとの競合対策)。
- **ディレクトリ**: `{vault}/{folder}/Daily/` と `{vault}/{folder}/Items/`。
- **ファイル名規則**:
  - Daily: `YYYY-MM-DD.md`(digest 生成日)
  - Items: `YYYYMMDD-{item_id}-{slug}.md`。slug はタイトルから Obsidian 禁止文字(`/\:*?"<>|#^[]`)を除去し40字で切る。item_id を含めるので slug 衝突は無害(一意性は id が担保)。
- **Daily ノート**: frontmatter(`date`, `item_count`, `degraded`)+ digest Markdown 全文。各アイテムから `[[YYYYMMDD-{id}-{slug}]]` で Items へ wikilink。
- **Items ノート**の構造:

```markdown
---
id: 1234
title: "…"
url: https://…          # DB の正規 URL のみ
source: TechCrunch
source_type: rss        # x / rss / manual
published: 2026-07-04
digest_date: 2026-07-05
score: 0.82
reaction: null          # フェーズ2で 👍/🔖/✅/👎 を書き戻す予約フィールド
---
<!-- GENERATED:BEGIN -->
## 要約
…(自作要約)
## 抜粋
…(RSS/manual のみ。X 由来は保持ポリシー上ここを出力しない)
## 推薦理由
…
<!-- GENERATED:END -->

(この下は手書きメモ領域。再生成でも保持される)
```

- **frontmatter の生成規則**: 外部由来文字列(title・url)を含むため、文字列連結で組み立てず **YAML シリアライザで生成する**(全値クオート+エスケープ。改行・引用符入りタイトルによる frontmatter 破壊や任意キー偽造 — 例: フェーズ2で意味を持つ `reaction` の注入 — を構造的に防ぐ)。
- **GENERATED マーカーの防御**: GENERATED 領域へ書き込む前に、生成コンテンツ(LLM 出力は untrusted 本文の影響下にある)から `<!-- GENERATED:BEGIN -->` / `<!-- GENERATED:END -->` のマーカー文字列を除去してから書く(境界誤認による手書きメモ破壊の防止)。
- **再生成ポリシー**: ファイルが存在すれば GENERATED マーカー内だけ置換、frontmatter は機械管理キーのみ更新 — 手書きメモを絶対に壊さないための境界。マーカー欠損時は上書きせず `.conflict.md` を並置して警告(fail-safe)。
- **反応の書き戻し**: MVP では実施しない(signals は SQLite のみ、PROPOSAL のスコープ)。frontmatter `reaction` と GENERATED 領域の分離が済んでいるため、フェーズ2では「frontmatter の1キー更新」だけで実現できる — これが本設計で仕込む唯一の拡張点。
- **保持ポリシー**: Obsidian に置くのは自作要約+メタデータ+URL+(RSS/manual のみ)抜粋。X 由来は本文・抜粋を置かない(Day 0 の Developer Agreement 確認で緩和されうる)。

## 8. 他コンポーネントとの境界

- 入力: `digest` コンポーネントが `digests` 行(Markdown + item_ids)を確定してから本層を呼ぶ。本層は選別・要約に関与しない。
- 出力: 本層が書くのは `signals` / `telegram_state` / `digest_messages` / `digests.delivery_status`・`sent_at` と Vault ファイルのみ。`items` への書込は URL 投入(manual source 経由)だけ。
- 失敗時: 配信失敗・Vault 書込失敗は watchdog(設計 05 想定)への通知対象。本層は例外を握りつぶさず終了コードで返す。
