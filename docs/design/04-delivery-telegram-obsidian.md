# 設計 04: 出口層 — Telegram 配信 / Obsidian 蓄積

ステータス: Draft v1(2026-07-05)/ 正本の企画: `docs/PROPOSAL.md`

対象コード: `app/delivery/telegram.py`(送信)、受信デーモン(`intel poll`。CLI 名の正本は設計 00 §3 / 05 §1.2)、`app/export/obsidian.py`。入力は `digests`(生成済み Markdown と item_ids)と `items` / `analyses`。出力は `signals` への記録と Vault へのファイル書き込み。

## 1. Telegram 全体方針

- Bot API は **httpx 直叩き**(共通規約)。接続は **long polling 固定**、`getUpdates` の offset は SQLite の専用テーブル(`telegram_state`。DDL は 01 §3)に永続化する — デーモン再起動で取り漏らし・二重処理しないため。
  - **offset を進める条件(GH#181)**: update を 1 件処理するごとに offset を保存する。受信の内容を **DB の一時的な失敗**(ロック競合で busy_timeout を超えた `database is locked/busy`。朝バッチの VACUUM・backup と重なったとき)で保存できなかった update は offset を進めず、30s 待ってから同じ offset で取り直し、Telegram が再配達した同じ update を再処理する(後ろの update も一緒に再配達される)。同じ update が続けて失敗するときは、やり直しの上限(回数は 05 §8)を超えたところで **隔離** して進める: poller.log に WARN 1 行(update_id・update の種別・エラーの種類だけ。本文は残さない。§6 と同じ)を残し、offset を進める。それ以外の失敗(不正な形の入力・ロック以外の DB エラー・返信の送信の失敗など)は待っても直らないので、今どおりログに残して offset を進める(poison update を無限に再処理しない)。callback(§3)の書き込みの失敗(signals の INSERT・深掘りカードのボタンの `dig_questions` への記帳)は、ロック競合でも再処理の対象にしない: §3 の警告 answer(`⚠️ 記録失敗、もう一度` など)で本人にやり直しを促して処理を終え、offset を進める。answerCallbackQuery は 1 callback に 1 回しか返せないので、待って記帳し直すと、返した警告と記帳の結果が食い違うため。
  - **再配達の冪等性**: offset は処理の後に保存するので、その間に止まる(または offset の保存がロック競合で失敗する)と同じ update が再配達される。感想(§5。`item_notes` / `digest_notes`)は受信したリプライの `chat_id`・`message_id` を一意キーにし(01 §3。GH#193)、記帳の 1 文で処理済みかを判定する(記帳済みなら記帳も返信もしない)。深掘りカードへの質問は `dig_questions` の UNIQUE(01 §7)で同じく 2 回受け付けない。signals(§3)は追記型で、重複は集計側で吸収する。
- parse_mode は **HTML** を採用 — MarkdownV2 はエスケープ対象が多く事故りやすい。エスケープ規定: **テキストノードは `& < >` の3文字、属性値(`href` 等)はさらに `"` と `'` も**エスケープする(canonical_url は外部制御可能であり、`"` 入り URL による属性破壊を防ぐ)。
- 本文由来の文字列(タイトル・要約・理由)は**必ず HTML エスケープ**して埋め込む。URL は LLM 出力でなく **items テーブルの正規 URL のみ**(PROPOSAL のインジェクション対策)。scheme は 01 §4 の allowlist(http/https のみ)で挿入時に保証済み。

## 2. 朝ダイジェストのメッセージフォーマット

構成は **ヘッダ1通+アイテム最大 `digest.max_items` 通(既定10。設計 03 §6)**(1アイテム=1メッセージ。ボタンをアイテム単位に付けるため)。

- ヘッダ: `📡 2026-07-05(土)のダイジェスト — 8件` + degraded 時は `⚠️ 本文抽出失敗/分析縮退あり` を付記。トレンド節(GH#75)に続けて **🔖 保存節(GH#99)** を同梱: `digests.bookmark_item_ids` のタイトルリンク(title_ja 優先・80字切り・属性エスケープ)を保存順に、表示上限 10 件(超過は「他 N 件は Items ノートへ」)。ランキング外の must-include 別枠で、アイテムメッセージ(反応ボタン)は作らない = KPI 分母に入らない。
- アイテム1通のテンプレート(上から順に):

```
{順位}. <b>{タイトル}</b>
<a href="{正規URL}">{ソース名}</a>{published_at があれば ・M/d}
{要約 2〜3文}
🎯 {推薦の理由 1文}
💡 {自分への示唆 1文}
```

- **見出しの言語**: `{タイトル}` は **analyses.title_ja(日本語見出し。analyze-v002)優先・NULL(v001 世代 / degraded)は items.title に fallback**。追い掛け(🔖 深掘りした?)の引用タイトルも同じ規則(設計 01 §3 / 03 §3.3)。
- **推薦の理由**(GH#90): `🎯` 行は `analyses.recommendation_reason`(analyze の reason = なぜ私に価値があるか)。どの角度(すぐ試せる Tips / 課題に直結 / 影響が大きい など)で推薦したかを digest の上で分かるようにする。category は 4 値で大半が「その他」に落ち角度を表せないため、タグではなく理由の文で伝える。degraded の分析では理由欄が記録用の定型文なので出さない。理由・示唆が空なら、その行ごと省く。
- **長さ制限**: Telegram は1通4096字だが、安全側で **1通1000字(概算)を上限**にし、超過分は要約末尾を `…` で切り詰める(URL・推薦の理由・示唆は削らない。タイトルは 300 字で切る = 実質ほぼ削らない)— 制限は要約で吸収するのが読みやすさを壊さない。
- 各アイテムに InlineKeyboard 1行4ボタン: `👍 価値あり / 🔖 深掘り / ✅ 既知 / 👎 不要`。
- 文体例(1件分、日本語ダイジェストのトーンの正):

> 2\. **Anthropic、企業向け Claude に監査ログ API を追加**
> [TechCrunch ・7/4]
> エンタープライズ管理者が全メンバーの利用ログをエクスポートできる API が公開された。SOC2 対応を理由に導入を渋っていた企業の障壁が一つ減る。料金プランは Enterprise のみ。
> 🎯 組織で AI ツールを入れるときの壁だった監査の要件に直結する。
> 💡 「ログが取れないから」と見送った AI ツールの導入を、もう一度検討する材料になる。

- **配信の冪等性**: 送信完了時に `digests.delivery_status='sent'` + `sent_at` を記録し(語彙は 01 §3 に統一。delivered_at という別名は使わない)、`intel deliver` は既配信の digest を再送しない(再実行安全)。送信途中失敗時は未送信アイテムのみ再送できるよう、送信済み message_id をアイテムごとに `digest_messages`(01 §3)へ記録する。
  - **配信の直列化**(GH#192): 台帳の確認から送信・記帳までを、同じ DB への配信どうしでプロセス間に直列化する(DB の隣の `<DB>.deliver.lock`(既定 `intel.db.deliver.lock`)を flock。migration の `<DB>.migrate.lock` と同じ名付け。01 §10)。朝バッチの deliver 段と手動の `intel deliver` / `intel run` が重なっても、後の側は先の側が終わるまで待ち、その記帳を読んでから未送信だけを送る(先の側が送り終えていれば配信済みとして skip)。
  - **送信後の記帳の失敗**(GH#225): 送信が成功して message_id を受け取った後に台帳の INSERT が失敗したら(ディスクの一時的な異常など)、送り直さず記帳だけを 3 回までやり直す(待ちは 1s→2s→4s。制約違反などはやり直さない)。それでも記帳できなければ、その message_id を ERROR のログ(朝バッチは `morning.log`)に残して配信を止める(先のアイテムは送らない。書ければ `delivery_status='failed'`)。台帳に無い message は再走で未送信として送り直され、感想のリプライの帰属(§5)も引けないため、再走の前にログの message_id を台帳へ入れて復旧する(手順は runbook §15)。送信と記帳の間でプロセスが止まったときは message_id が残らず、再走で送り直しうる(この対処の外)。

## 3. callback_query 処理(受信デーモン)

- **callback_data 形式**(64バイト制限内・ASCII のみ):
  - 反応ボタン: `s:{item_id}:{up|dig|knw|no}`(👍🔖✅👎に対応)
  - 夜アンケート: `q1:{YYYYMMDD}:{y|n}` / `q2:{YYYYMMDD}:{分}` / 深掘り追い掛け: `f:{item_id}:{y|n}`
  - 深掘りカードのボタン(GH#141): `d:{dig_result_id}:{s|m}`(📝 要約 / 📖 詳しく)。押されたメッセージがそのカード(`dig_results.message_id`・owner の chat)であることを確かめてから台帳 `dig_questions` に積む(signals には書かない。設計 06 §6)。🔗 元記事は URL ボタンで callback を持たない
- 処理順は **①検証+signals に INSERT → ②結果に応じて answerCallbackQuery(記帳成功 = `記録しました` / 検証失敗・期限切れ・DB 書込失敗など**記帳できなかった場合** = 警告 answer)→ ③ editMessageReplyMarkup で選択状態を反映**(選ばれたボタンに `✓` を付けた盤面に差し替え)。answerCallbackQuery は 1 callback につき 1 回しか返せないため、記帳の成否で answer 文言を分けるには記帳を answer より先に行う必要がある(ローカル SQLite はミリ秒オーダーで、10秒失効に十分間に合う)。
- **INSERT 前の検証**: パースした item_id が items に実在し、かつ**いずれかの `digests.item_ids` に含まれる**ことを確認してから signals に書く(signal_date の帰属=その digest_date もこの照合で同時に解決)。不一致・不正形式の callback_data は記帳せずログのみ(古いメッセージや偽造データで KPI 分母外に記帳されるのを防ぐ)。
- **押し直し許容**: signals は追記型(append-only)で記録し、KPI 集計時は item ごとの最新レコードを採用 — 誤タップを訂正可能にしつつ履歴を失わない。
- answerCallbackQuery の text は短く(例: `👍 記録しました`)。記帳できなかった場合は警告を返す: 検証失敗(digest 帰属なし・🔖 なし)や DB 書込失敗は `⚠️ 記録失敗、もう一度`、夜アンケートの期限切れ(翌日12:00 超)は `⚠️ 回答期限(翌日12:00)を過ぎています`。未対応形式の callback_data は text なしの空 answer(スピナー停止のみ)。

## 4. 夜アンケートと 🔖 追い掛け

- 送信ロジックは `delivery/telegram.py` の関数として実装するが、**起動は受信デーモン(`intel poll`)の内蔵タイマー(21:30 JST 仮、config で変更可)**とする(設計 05 §1.2 と同一方式。plist を2枚に抑え、朝バッチ以外の Telegram 送信経路をポーラーに集約する。別 launchd ジョブ+`--survey` 方式は採らない)。
- 質問1「今日、自分から情報を探しに巡回した?(ダイジェストを読むのは含めない)」(はい/いいえ)、質問2「今朝のダイジェストを読むのにかかった時間は?(ざっくりでOK)」はボタン `5/10/15/20/30分` の5択 — 自由入力より1タップ原則を優先。文言の正本は PROPOSAL §夜の1タップアンケート(2026-07-11 調整。測定定義は不変)。
- 翌日 12:00 JST までの遅延回答は callback_data 内の日付で正しい日に記帳される(送信日時に依存しない)。未回答の fail-closed 集計は手動集計側の責務(本層は記録のみ)。
- **🔖 追い掛け送信は MVP に含める(最小形)**: 夜アンケート送信の直後に、**前日 🔖 が付き `followup_dig` 未記録のアイテムごとに「深掘りした?(はい/いいえ)」を1通ずつ送る**(callback_data `f:{item_id}:{y|n}`、記録は 01 §7 の `followup_dig`)。理由: フェーズ2境界の進出条件「🔖の50%以上を実際に深掘り」(PROPOSAL §フェーズ2境界が正)は追い掛け記録がないと MVP 期間中に測定できない。※旧記載「MVP スコープ外」は Codex レビュー(2026-07-05)で PROPOSAL 側に合わせて修正。
- **深掘り動線(GH#88・ユーザーフィードバック 2026-07-14)**: 追い掛けの引用タイトルは **canonical_url へのリンク**にする(その場から対象を読みに行ける)。さらに追い掛けの**前に「未消化の 🔖 一覧」を1通**送る — 対象 = 最新 reaction が 🔖 のままで `followup_dig=yes` が無い累計(「いいえ」回答済みは残す = まだ深掘っていない)。新しい 🔖 順・上限 15 件(超過は「他 N 件」と件数のみ)・0件の日は送らない。動線ゼロだと境界進出条件(50% 深掘り)が「需要の不在」でなく「実行不能」を測ってしまうため、計測を歪めない最小動線として追加。**X ブックマークの保存アイテム(GH#99)も合流**する(`x_signals.item_id` 紐付き。Telegram の訂正があっても X 上のブックマークが実在する限り残す・同一 item は 1 行に dedupe・マーク時刻の新しい順)。ブックマーク item の消化(followup_dig)動線は、深掘りカードへの take の記録で閉じる(GH#106。take を返すと `followup_dig=yes` になり一覧から消える。設計 06 §6)。**一覧の行頭番号は item id**(GH#104。表示位置の連番だと日ごとに変わりリプライ指定と食い違う)で、末尾にリプライ例(「{id} を◯◯の観点で」= 登録 / 「{id} はパス」= 除外)を実在 id で示す。一覧の message_id は `dig_backlog_messages` に記録し、**一覧へのリプライは深掘り指定**として `dig_queue` へ反映する(パーサと upsert 規則は設計 01 §7 末尾・設計 06 §2)。
- 実装が Week 2 に収まらない場合の格下げ先: 送信を手動(自分宛ての固定リマインダ)にし、回答を手動 SQL で `followup_dig` に記録して判定材料とする(1行運用)。

## 5. URL 投入受付

- デーモンは owner からの**平文メッセージ中の http/https URL をすべて抽出**し、`sources`(type=manual)経由で `items` に投入。既存 URL(**canonical_url 一致のみ。**content_hash は使わない — manual は本文が無く空ハッシュになるため。01 §5〜6 / 02 §4)は再投入せず「投入済み」と返信。
- 応答例: `✅ 1件受け付けました。明朝のダイジェストで分析します`。URL を含まないメッセージには `URL を送ると翌朝のダイジェスト候補になります` と定型応答(コマンド体系は作らない — MVP の操作は URL 投入だけで足りる)。
- **感想キャプチャ(GH#92 / GH#96)**: メッセージが digest の**記事メッセージへのリプライ**(`reply_to_message.message_id` が `digest_messages` の item 行に一致)なら `item_notes`、**ヘッダメッセージへのリプライ**(item_id IS NULL 行に一致)なら `digest_notes` に記録して ack。同じリプライ(`chat_id`・`message_id`)の再配達では記録も ack もしない(§1。GH#193)。この経路では URL 抽出をしない(感想内の URL は感想の一部)。未消化一覧へのリプライは深掘り指定(GH#104。`dig_backlog_messages` 台帳で認識)。深掘りカード・その返事へのリプライは、感想(item_notes)として記録したうえで質問として `dig_questions` に積む(設計 06 §6・05 §1.2)。これらのリプライの振り分けは owner の DM(`owner_chat_id`)のメッセージだけ。追い掛け等**台帳に無いメッセージへのリプライ・非リプライは従来の URL 投入フロー**。KPI(reaction)には記帳しない。Obsidian への投影は朝バッチ export(§7。記事感想 = Items ノート / 日次感想 = Daily ノート)。後から付いた感想で Items ノートを書き直す追いパス(§7 の `export_pending_notes`)の対象は、digest に記事メッセージとして載った item だけ(`digest_messages` の記事の行を手がかりにするため)。🔖 保存節にだけ載った X ブックマークや、digest に載っていない item への感想(深掘りカードへのリプライなど)は `item_notes` に残るが、ノートには出ない(take は解説ノートに追記される。設計 06 §6)。例外: その日の digest を export し直したとき(`intel run` の再実行)は、🔖 保存節の item のノートもその時点の感想を全部出す(§7 の感想節は DB の純関数)。

## 6. 認可

- config.toml の `[telegram] owner_user_id` / `owner_chat_id` を **allowlist の正**とする。message は `from.id`、callback_query も `from.id` で検証(chat_id だけではグループ転送経由をすり抜けるため)。
- 不一致の update は **poller.log への WARN 1行(JSON Lines: `from.id`・update 種別・受信時刻のみ。本文ペイロードは保存しない)で記録して破棄**(応答も返さない — bot の存在を確認させない)。第三者の URL 注入と KPI 汚染の遮断が目的。専用 DB テーブル(rejected_updates)は作らない — 1人運用で照会頻度がほぼゼロのデータに untrusted ペイロードの無制限蓄積面を作らない。頻発が観測されたら初めてテーブル化を検討する。

## 7. Obsidian 出力

- **設定**: config.toml の `[obsidian] vault_path` + `folder`(既定 `SignalForge`)。パスはハードコード禁止。書込は **一時ファイル→rename のアトミック書き**(同期ツールとの競合対策。防ぐのは書きかけのファイルが残ることで、読み取り後に保存された変更との競合は下の「読み取り後の変更の検出」で扱う)。
- **ディレクトリ**: `{vault}/{folder}/Daily/` と `{vault}/{folder}/Items/`。
- **ファイル名規則**:
  - Daily: `YYYY-MM-DD.md`(digest 生成日)
  - Items: `YYYYMMDD-{item_id}-{slug}.md`。slug は**原文タイトル(items.title)**の Obsidian 禁止文字(`/\:*?"<>|#^[]`)と空白の連続を `-` に置き換え、両端の `-` を落として 40 字で切る(title_ja 導入後もファイル名規則は不変。frontmatter の `title` のみ title_ja 優先)。item_id を含めるので slug 衝突は無害(一意性は id が担保)。
- **Daily ノート**: frontmatter(`date`, `item_count`, `degraded`)+ digest Markdown 全文。各アイテムから `[[YYYYMMDD-{id}-{slug}]]` で Items へ wikilink。🔖 保存節の item(GH#99 = `digests.bookmark_item_ids`)も配信 item と同様に Items ノート化し wikilink に載せる(=「保存」の実体。順序は配信順 → 保存順)。
- **Items ノート**の構造:

```markdown
---
id: 1234
title: "…"
url: https://…          # DB の正規 URL のみ
source: TechCrunch
source_type: rss        # x / rss / manual
published: 2026-07-04T09:30:00Z   # items.published_at(ISO タイムスタンプ)をそのまま
digest_date: 2026-07-05
score: 82.0             # analyses.total_score(0〜100)をそのまま
reaction: null          # フェーズ2で 👍/🔖/✅/👎 を書き戻す予約フィールド
---
<!-- GENERATED:BEGIN -->
## 要約
…(自作要約)
## 抜粋
…(RSS/manual のみ。X 由来は保持ポリシー上ここを出力しない)
## 推薦理由
…
## 示唆
…(自分への示唆 1文)
## 感想(YYYY-MM-DD)
…(Telegram リプライの感想。GH#92。0件なら節ごと出ない・複数件は節を重ねる)
<!-- GENERATED:END -->

(この下は手書きメモ領域。再生成でも保持される)
```

- **frontmatter の生成規則**: 外部由来文字列(title・url)を含むため、文字列連結で組み立てず **YAML シリアライザで生成する**(`yaml.safe_dump`。クオート形式はシリアライザ任せで、必要な値だけ適切にクオート・エスケープされる。改行・引用符入りタイトルによる frontmatter 破壊や任意キー偽造 — 例: フェーズ2で意味を持つ `reaction` の注入 — を構造的に防ぐ)。
- **GENERATED マーカーの防御**: GENERATED 領域へ書き込む前に、生成コンテンツ(LLM 出力は untrusted 本文の影響下にある)から `<!-- GENERATED:BEGIN -->` / `<!-- GENERATED:END -->` のマーカー文字列を除去してから書く(境界誤認による手書きメモ破壊の防止)。
- **再生成ポリシー**: ファイルが存在すれば GENERATED マーカー内だけ置換、frontmatter は機械管理キーのみ更新 — 手書きメモを絶対に壊さないための境界。マーカー欠損時は上書きせず `.conflict.md` を並置して警告(fail-safe)。
- **読み取り後の変更の検出(GH#189)**: ノートを読んだあとに本人や同期ツールが保存した変更を、古い内容から作った新版で上書きしないための仕組み。一時ファイルを書いたあと rename の直前に、読んだときの内容と今の内容(ファイルの有無を含む)をバイト列で比べ、変わっていれば上書きしない。読み直して 1 回だけやり直し(新しい内容の手書き領域と frontmatter をもとに作り直す)、それでも変わっていれば生成した新版を `.conflict.md`(既にあれば `.conflict-2.md`、`.conflict-3.md` と続く空いている名前。マーカー欠損時と同じ規則)へ退避し、ノート名を添えて WARN を出す。退避したノートは未更新の扱いで、感想の追いパスは `exported_at` を立てず翌 run で再試行する(マーカー欠損時と同じ契約)。新規作成も同じで、rename の直前にファイルができていれば読み直して既存ノートとして扱う。
- **保証の範囲**: 比べてから rename までの短い窓に保存された変更は検出できず、ファイル全体の置換で失われうる(OS の rename に「内容が変わっていなければ置き換える」という条件は付けられないため)。手書きを確実に守るには生成部分と手書き部分を別ファイルに分ける必要があるが、今のノートの形(同居)を保ち、検出と退避で窓を短くするにとどめた(GH#189 の判断)。export は朝バッチで 1 日 1 回、書くのは本人 1 人の Vault なので、この窓に保存が重なる頻度は低いと見ている。
- **感想の投影(GH#92 / GH#96)**: 感想の正本は `item_notes` / `digest_notes`(01)で、GENERATED 領域の「## 感想」節は **DB の純関数**(全感想を毎回描画 = 再生成・再 export で消えない)。記事感想は Items ノート、日次感想は Daily ノートに投影。既 export のノートに感想が付いた場合は export 段の追加パス(`export_pending_notes`)が該当ノートだけ再描画し(item は `digest_messages`→`digests` で digest_date を解決・digest は digests.digest_date で直接)、`exported_at` で差分管理する。感想を GENERATED 領域内で直接編集した場合は次回再描画で DB 側の内容に戻る(編集は手書き領域で)。
- **反応の書き戻し**: MVP では実施しない(signals は SQLite のみ、PROPOSAL のスコープ)。frontmatter `reaction` と GENERATED 領域の分離が済んでいるため、フェーズ2では「frontmatter の1キー更新」だけで実現できる — これが本設計で仕込む唯一の拡張点。
- **保持ポリシー**: Obsidian に置くのは自作要約+メタデータ+URL+(RSS/manual のみ)抜粋。X 由来は本文・抜粋を置かない — **確定(Day 0 の Developer Agreement 確認 2026-07-06、Issue #2)**。規約上は自分専用・非公開への抜粋表示自体は許諾範囲だが、Obsidian は恒久保存層であり「X 側の削除・変更との同期」義務(Policy: Content compliance)を構造的に満たせないため緩和しない。恒久保存してよいのは Post ID / URL / 自作要約(analyses.summary)+メタまで。位置情報は本文と切り離して保存しない(Agreement III.E。現設計は geo フィールドを取得自体しないので追加対応なし)。

## 8. 他コンポーネントとの境界

- 入力: `digest` コンポーネントが `digests` 行(Markdown + item_ids)を確定してから本層を呼ぶ。本層は選別・要約に関与しない。
- 出力: 本層が書くのは `signals` / `telegram_state` / `digest_messages` / `item_notes`・`digest_notes`(感想。GH#92/#96)/ `dig_backlog_messages`・`dig_queue`(深掘りの指定。GH#104)/ `dig_questions`(カードへの質問。GH#126)/ `digests.delivery_status`・`sent_at` と Vault ファイルのみ。`items` への書込は URL 投入(manual source 経由)だけ。
- 失敗時: 配信失敗・Vault 書込失敗は設計 05 §4 の失敗通知で知らせる(朝バッチの段の失敗 = 朝バッチ失敗通知。watchdog が見るのはダイジェストの未生成・未配信と X ソースの連続失敗だけで、Vault 書込失敗は見ない)。本層は例外を握りつぶさず終了コードで返す。
