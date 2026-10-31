# 設計 02: コネクタ(X / RSS / 手動URL)

ステータス: Draft v1(2026-07-05)/ 正本の上位文書: `docs/PROPOSAL.md`

対象: `app/connectors/` と `intel collect`。データモデルの正本は 01(DB設計)、本文抽出は extraction/ の管轄(コネクタは本文抽出をしない)。

## 1. 共通 Connector インターフェース

```python
# app/connectors/base.py
@dataclass(frozen=True)
class RawItem:
    external_id: str | None   # ソース内の自然キー(tweet id / RSS guid)。無ければ None
                              #   (guid の無い RSS は collect が正規化 URL の hash で補う。§3)
    url: str                  # 正規化前URL。正規化は collect 側で一元化
    title: str | None
    author: str | None
    published_at: datetime | None   # UTC
    raw_content: str | None   # ソースが本文を同梱する場合のみ(RSS content 等)。抽出層へのヒント。
                              #   collect が items.raw_payload(01 §6 の JSON 形・サイズ上限あり)に
                              #   保存し、extraction は DB 経由で参照する(直接受け渡ししない)
    meta: dict = field(default_factory=dict)  # ソース固有情報(RT 種別など)。JSON保存

@dataclass(frozen=True)
class FetchResult:
    items: list[RawItem]
    next_cursor: dict         # sources.cursor に JSON 保存する不透明な辞書
    stats: FetchStats         # items_fetched, api_reads, estimated_cost_usd(collect_runs へ記録)
                              #   + http_status, duration_ms(ログのみ)
    partial_error: Exception | None = None   # 部分取得のあとの失敗(GH#183・GH#182)。collect は items と
                              #   next_cursor を保存しつつ、ソースの失敗として記録する(下の「記録」)
    budget_exhausted: bool = False   # X の当日の読取の枠が残っておらず、読取を要求しなかった(GH#178)。
                              #   collect はソースの成否の状態を変えない(下の「記録」・§2)

class Connector(Protocol):
    source_type: str          # "x" | "rss"
    def fetch(self, source_row: Source, cursor: dict) -> FetchResult: ...   # Source は app.models
```

- **fetch は読み取り専用・副作用なし**(DB に触らない)。items への書き込み・URL正規化・重複排除・カーソル保存はすべて `intel collect` が行う。理由: 冪等性の担保箇所を1箇所に集約し、コネクタ追加時にバグ面を増やさない。
- **冪等性契約**: 同じ cursor で fetch を何度呼んでも、collect 後の items テーブルは同一状態になること。担保は items の UNIQUE 制約(`source_id + external_id` と `canonical_url`)+ `INSERT OR IGNORE`(内容ベースの 24h dedup は 01 §6。正規化テキストが空/極端に短いアイテムは対象外)。コネクタ側は重複を返してよい。
- **カーソル更新のタイミング**: そのソースの全 RawItem の INSERT が成功した後、同一トランザクションで `sources.cursor` を更新。途中クラッシュ時はカーソル未更新→次回重複取得→UNIQUE で吸収。理由: 「カーソルだけ進んで items が欠ける」取りこぼしを構造的に排除(欠落方向より重複方向に倒す)。
- **エラー分類(全コネクタ共通)**:
  - `RetryableError`(接続失敗・timeout・5xx・429): リトライは**設計 05 §8 の共通規約に従う**(2回・指数バックオフ 1s→4s + jitter。429 は Retry-After 優先)。リトライ尽きでそのソースを当日スキップ。ただし X で部分取得のあとに尽きたときは、取得ぶんを保存しつつ失敗として記録する(下の「記録」・§2。GH#182)。
  - `FatalError`(401/403・設定不備): リトライせずスキップ、doctor 警告対象。X で部分取得のあとに 401/403 が来たときも致命のまま扱うが、それまでの読取数と費用は台帳に残す(下の「記録」・§2。GH#178)。
  - いずれもソース単位で握りつぶし、**他ソースの collect は必ず継続**(縮退の基本形。X が死んでも RSS+手動URLでダイジェストは出る)。
- **記録**: fetch 1回ごとに `collect_runs`(source_id, started_at, finished_at, status, items_fetched, api_reads, estimated_cost_usd, error)へ1行。テーブル定義の正本は 01(語彙も 01 に従う)。http_status・duration_ms はログ(JSON Lines)のみで DB には持たない(1人運用のデバッグはログで足りる)。`sources` には last_success_at / last_error_at / consecutive_failures を反映する(成功で 0 リセット・失敗で +1。01 §3)。部分取得のあとの失敗(`partial_error`。X 検索の後ろのスロットの失敗と、ホームTL の 2 ページ目以降の失敗。HTTP の失敗も通信のリトライ尽きも含む。§2)は、items とカーソルを保存しつつ失敗として記録する(collect_runs は件数・費用つきの `status='error'`。sources は失敗で +1 し、last_success_at は変えない。GH#183・GH#182)。部分取得のあとの 401/403(X)は `FatalError` のまま失敗として記録し、items とカーソルは保存しないが、例外に添えた読取数と費用(`FatalError.api_reads` / `estimated_cost_usd`)は collect_runs に残す。fetch は返したが保存で失敗したときも、取得の統計の読取数と費用を残す(どちらも日次の読取上限の当日の使用量と月次の費用を過少にしないため。GH#178)。X の当日の読取の枠が残っておらず読取を要求しなかった実行(`budget_exhausted`)は、collect_runs は 0 件の `ok` で残し、sources はカーソルだけを保存して成功の時刻・直近のエラー・連続失敗数を変えない(復旧を確かめていないので、失敗の続きを成功で消さない。GH#178)。
- **SSRF ガード**: 短縮 URL 展開・本文抽出を含む外部 URL fetch の全経路は、設計 05 §8 の共通 HTTP クライアント規約(global でないアドレスへの接続拒否 = is_global allowlist 方式、リダイレクト各ホップで再検査、検証済み IP へのピン留め)に従う。
- 拡張点: 新ソースは Connector 実装+sources 1行の追加のみ。スケジューリング分岐(ソース別頻度)は現時点で作らない(朝バッチ1回で十分)。

## 2. X コネクタ(`connectors/x.py`)

> **ADR 0001(2026-07-10)で拡張・訂正**: Day 0 実測(実効単価 ≈ $0.004/件・rate limit home 180/15min)を受け、X ソースは「ホームTLのみ」から **ホームTL + AI 検索(`/2/tweets/search/recent`・3スロット固定)** に拡張(検索の詳細は ADR 0001 / Issue #57)。旧記載の rate limit「5 req/15min」「単価 $0.005 想定」は下記のとおり実測値に更新。読取コストは**ホームTL + 検索の合算日次上限**で月 $20 以内に管理する(3スロット固定はクエリ本数の上限でありコスト上限ではない。ADR 0001「予算配分」)。

- **エンドポイント**: `GET /2/users/:id/timelines/reverse_chronological`(逆時系列ホームTL)。パラメータ: `max_results`(上限100。残枠が100未満なら `daily_read_cap - reads` = cap を1件も超えないため), `since_id`, `pagination_token`, `tweet.fields=created_at,author_id,referenced_tweets,in_reply_to_user_id,note_tweet`(note_tweet = 長文ポストの全文。text の280字切り詰めで記事系の中身が analyze に渡らない問題の解消 = GH#94。同一読取のフィールド追加で課金不変), `expansions=author_id`。実効単価 **≈ $0.004/件**(Day 0 実測。PROPOSAL §Day 0 ゲート)。AI 検索エンドポイントは下記「AI 検索」(Issue #57 で追加)。
- **カーソル**: `cursor = {"since_id": str}`。ページングは `pagination_token` で新→旧に辿り、(a) since_id に到達、(b) **日次取得上限**(config `x.daily_read_cap`、コスト逆算値。実測 $0.004 なら 130。JST の暦日の上限で、同じ日の実行で共有する = 下の「日次上限の共有」)到達、(c) rate limit 残なし、のいずれかで打ち切る。打ち切りで捨てるのは古い側であり、見逃し防止は非目標なので許容。次回 since_id は**今回取得した最大 tweet id**(打ち切り時も同じ。古い側の穴を追わない)。**2 ページ目以降の失敗(429・5xx などの HTTP の失敗と、通信のリトライ尽き)は打ち切りと分け、取得ぶんを保存しつつソースの失敗として記録し、since_id は据え置く**(GH#182。`FetchResult.partial_error`。検索と同じ形 = GH#183)。理由: 例外で取得ぶんを捨てると、その読み取りの費用も collect_runs に残らず月次の費用が過少になる。失敗で取れなかった古い側は方針で捨てたものではないので、翌日に旧 since_id から取り直す(取得済みぶんは UNIQUE で吸収。再読課金は daily_read_cap の上限までで許容)。1 ページ目の失敗は従来どおり例外(0 件成功で障害を隠さない)。2 ページ目以降の 401/403 は致命の失敗(`FatalError`)のまま送出し、取得ぶんは保存せず since_id も据え置くが、それまでの読取数と費用を例外に添えて collect_runs に残す(GH#178。下の「日次上限の共有」の台帳から読取を漏らさない)。
- **rate limit**: **実測 home 180/15min**(Day 0。旧記載 5/15min は誤り)。`x-rate-limit-remaining` を見て残0なら打ち切り(15分待ちはしない。朝バッチ1回で取り切れる量に cap を設計する前提)。実測上は cap 130 が rate limit の制約を受けない。
- **コスト記録**: 取得件数 × config `x.unit_price_usd`(Day 0 実測値を設定)を collect_runs.estimated_cost_usd に記録。doctor が月次見込みを集計し、超過ペースで警告。
- **日次上限の共有(GH#178)**: `daily_read_cap` は 1 回の実行の上限でなく、**JST の暦日の上限**(ADR 0001 の合算日次ハード上限)。失敗のあとの手動の `intel run`(設計 05 §1 の再走)や単独の `intel collect` で同じ日に再実行しても、上限を超えて読まない。合成点(`app/cli.py` の `_build_connectors`)が台帳 `collect_runs.api_reads` の当日の合計を mode(home / search)ごとに出し(`x_reads_today`)、コネクタは上限からそれを引いた残りだけを読む(fetch は DB に触らない契約のまま = §1)。当日は `started_at`(UTC)を JST の暦日に直して決める(dig の日次上限と同じ暦日)。部分取得のあとの失敗(`status='error'`。途中の 401/403 で終わった行も、それまでの読取を記録する)の行の読取も数える(費用は掛かっている)。残りが 0 の日はタイムラインを読まず、since_id を据え置く(翌日、新しい側から取る。古い側は追わない方針のまま)。読まなかった実行は復旧を確かめていないので、ソースの成否の状態(成功の時刻・直近のエラー・連続失敗数)を変えない(`FetchResult.budget_exhausted`。失敗の続きを成功で消さず、「縮退」の警告から外さない)。限界: 台帳に読取が入るのは fetch の終わりなので、同時に走る 2 つの実行は互いの読取を見ない。fetch の途中の想定外の例外(分類済みの失敗でないもの。応答の形の誤りなど)で止まったときの、それまでの読取も台帳に残らない。
- **リツイート/リプライ**: RT は referenced tweet の URL に正規化して重複排除に乗せる。自分宛以外のリプライは既定で除外。meta に `retweet` / `reply` / `self_retweet`(自分の RT)を記録し、自分宛のリプライと自分の RT は事前選別の規則 3 が除外する(03 §2)。1 回の取得で同じ元ツイートが複数回出たら(他人の RT と自分の RT など)1 行にまとめ、どれかが自分の RT なら `self_retweet` を立てる(collect の重複排除は先着の meta だけを残すため、並び順で除外が変わらないように)。前日までの取得や検索ソースで先に入った行は書き換えない。理由: ノイズ率対策の最安の一手。
- **本文保持ポリシー(RSS と分離)**: tweet 本文は分析に必要なので documents に入れるが **TTL 付きキャッシュのみ・恒久保存しない**(恒久保存は自作要約+メタ+URL)。**確定済み(2026-07-06、Issue #2)**: `x_cache_ttl_days=7`、X 由来の抜粋は Obsidian に置かない(詳細は 01 §6 / 04 §7)。
- **縮退**: consecutive_failures ≥ 3 で doctor #14 が WARN、watchdog が Telegram に通知(自動 disable はしない。理由: 勝手に情報源が消えるより通知して人が `sources.enabled=0` を切る方が安全)。watchdog は直すか止めるまで毎朝 1 通(同一種別 1 日 1 通。設計 05 §4)。collect はソースの失敗を run の失敗にしないため、朝バッチ失敗通知には載らない(GH#124)。`x.enabled=false` の間は回数が進まないので知らせない。X 停止中もパイプラインはソース非依存で RSS+手動URLで継続。

### AI 検索(`connectors/x_search.py`・Issue #57)

- **エンドポイント**: `GET /2/tweets/search/recent`。パラメータ: `query`(括弧で囲んだスロット + 自動付加演算子), `max_results`(10〜100。**下限 10 は API 仕様**), `start_time`(watermark の時刻。`since_id` は送らない。下の「カーソル」), `end_time`(全スロット共通の取得の上限。下の「カーソル」), `next_token`(検索は `pagination_token` でなく `next_token`), `tweet.fields=created_at,author_id,note_tweet`(note_tweet はホームTL と同じ理由 = GH#94), `expansions=author_id`(RT/リプライはクエリ演算子で除外済みのため referenced_tweets は要らない)。認証はホームTL と同じ OAuth 1.0a(既存4キー。Bearer は使わず認証経路を1本に保つ)。署名・リトライは共有トランスポート(`XApiSession`)。
- **スロット**: config `[x_search].slots`(**最大3・入れ替え式**。ADR 0001)。4本以上・重複・空文字・括弧と自動付加分込み512字超(Basic tier のクエリ長上限)・括弧(引用符の外)や引用符が対応していないもの(囲んだ括弧が途中で閉じて除外句が全体に掛からない。GH#230)は ConfigError で fail fast。**slots 空 = 検索無効**(コネクタを配線しない)。スロット自動更新は作らない(更新提案は S2 = #59 の人間承認ゲート)。
- **クエリの自動付加**: 各スロットを括弧で囲んで `-is:retweet -is:reply` を付加し(`(スロット) -is:retweet -is:reply`)、RT・リプライを**課金前にサーバ側で除外**する。ホームTL の「RT は元ツイへ正規化・他人宛リプライ除外」と方針整合で、取ってから捨てると読取課金が無駄になるため。括弧で囲むのは、X の検索が AND を OR より先に結び付けるため: 括弧なしの `a OR b -is:retweet -is:reply` では除外が `b` の枝にしか掛からない(GH#230)。`lang:` 等の演算子や OR はスロット側に書いてよい(スロット内の結合はスロットに書いたとおりで、除外だけが全体に掛かる)。スレッド(著者の自己リプライ)も落ちるが MVP はノイズ削減を優先(RawItem.meta に `{"query": スロット}` を記録し、週次の再配分材料にする)。
- **予算**: `[x_search].daily_read_cap`(既定35)をスロット数で均等割(端数は先頭から。35 → 12/12/11)。スロット間の再配分はしない(多産クエリが他スロットを飢えさせない)。残枠が 10(max_results 下限)を切ったらそのスロットは打ち切り(**cap はハード上限**。超過方向に丸めない)。cap はホームTL と同じく JST の暦日の上限で、同じ日の実行が読んだぶん(collect_runs の当日の合計。上の「日次上限の共有」)を引いた残りを均等割する(GH#178。残り 25 なら 9/8/8 がどれも下限 10 を切るので、その日はもう読まない。どのスロットも要求しなかった実行は、ホームTL と同じくソースの成否の状態を変えない)。残りの割り当てが下限 10 に届かず 1 回も要求しなかったスロットがある実行は、ほかのスロットが取れても完走でないので watermark を据え置く(下の「カーソル」。残り 29 なら 10/10/9 で、3 本目を要求しない)。**ホームTL との合算は `[x].combined_daily_read_cap`(既定165)が単一ノブ**で、内訳合計が超えると起動時 ConfigError(ADR 0001 予算配分)。
- **カーソル**: `cursor = {"since_id": 全スロット横断の最大 tweet id}`。tweet id(snowflake)は時刻単調なので単一 watermark で足り、スロット入れ替え後もそのまま有効(新スロットは前回実行以降ぶんから取れる)。ただしスロットは順に取るので、取得の上限の時刻がスロットごとに違うと、先のスロットを取ったあと・後のスロットを取る前に先のスロットに入った新着が、後のスロットの最大 id(= 次回の since_id)の背後に隠れて恒久欠落する。そこで**全スロットの取得の上限を共通にする**(GH#184): 実行の開始の 30 秒前を `end_time` にして全スロット・全ページに送り、その境界までだけを取って watermark を進める(境界より後の新着は次回、どのスロットからも取れる)。search/recent は `end_time` を要求の時刻より 10 秒以上前に限り、指定しなければ要求の 30 秒前を上限にする。既定と同じ 30 秒にして、索引の遅れへの余裕を今までと変えない(手元の時計が X より 20 秒以上進むと 400 になり、失敗として記録される)。**search/recent は `since_id`・`until_id` を `start_time`・`end_time` と同じ要求で受け付けない**(400。GH#319): `end_time` を全要求に送るので、watermark は `since_id` として送らず、その snowflake の時刻を秒に切り捨てた `start_time`(その時刻を含む)にして送る。watermark と同じ秒の投稿は読み直しになり、collect の UNIQUE で吸収される(読み直しは同じ秒の数件で、その課金は許容)。**watermark は全スロット完走時のみ進める**: 途中打ち切り(失敗・rate limit 残0。当日の残り枠の割り当てが下限に届かず要求しなかったスロットがあるときも含む。GH#178)で進めると未実行スロットのその日ぶんが watermark の背後に隠れて恒久欠落するため、打ち切り時は据え置く(翌日、旧 watermark から再取得して UNIQUE で吸収。取得済みぶんの再読課金は検索 cap 上限までで許容)。スロット内の予算打ち切りで捨てる古い側はホームTL 同様に追わない。**search/recent は直近 7 日より古い since_id を 400 で拒否する**(ホームTL には無い制約。`start_time` も同じ 7 日窓に限られる): 停止明けで watermark が窓の外に出ると毎日 400 で永久に進まないため、**6 日(7 日に 1 日の余裕)より古い watermark は外して `start_time` なしで取得し WARNING を出す**(GH#121。窓の外の欠落は API 上取り戻せない)。外すのは送信だけで、打ち切り・0 件のときは上の規約どおり元の値を据え置く(次回も同じ判定で外れる)。失敗の扱いもホームTL と同方針: 累計0件での失敗は例外(0件成功で障害を隠さない)/ 部分取得後の失敗・rate limit 残0は打ち切り返却(残りスロットも当日分は諦める)。**部分取得後の失敗(400・402・429・5xx・通信のリトライ尽き)は、取得ぶんを保存しつつソースの失敗として記録する**(GH#183。通信のリトライ尽きは GH#182。`FetchResult.partial_error`。consecutive_failures が上がり、上の「縮退」の連続失敗の警告に載る)。後ろのスロットのクエリの誤りによる 400 が毎日続いても、先のスロットが取れているからと正常扱いで隠さないため。予算・rate limit 残0による打ち切りは失敗にしない(応答は正常)。部分取得後の 401/403 はホームTL と同じく致命の失敗(`FatalError`)のまま送出し、取得ぶんは保存せず watermark も据え置くが、それまでの読取数と費用は collect_runs に残す(GH#178)。
- **sources 行と識別**: **type='x' のまま2行運用**(purge の `x_cache_ttl_days`・Obsidian 抜粋なし・KPI 集計が無変更で検索アイテムにも効く)。識別は sources.config の JSON `{"mode": "search"}`。**config NULL = home**(既存ホームTL 行の互換)。mode 不正・不明キーは FatalError(タイポで home に化けてホームTL を二重取得・二重課金する事故を防ぐ)。collect の「type → Connector 1:1」契約(§1)は `XRouter`(connectors/x.py)が吸収する。検索ソースの登録 SQL:
  ```sql
  INSERT INTO sources (type, name, config) VALUES ('x', 'x:search', '{"mode": "search"}');
  ```
- **doctor #5 の整合検査**: `[x_search].slots` ⟷ mode=search 行の相互欠落(検索が黙って収集されない / collect で失敗し続ける)、同一 mode の複数 enabled 行(二重取得・二重課金)、mode 不正行を WARN で可視化。
- **月次上限**: 課金は credits 従量(Day 0 実測)のため検索専用の別枠上限は持たず、合算日次 cap × 30(165 × 30 × $0.004 ≈ $19.8/月)と doctor #9 の当月集計(collect_runs の estimated_cost_usd 合算。検索も自動で載る)で管理する。

### OAuth 2.0(PKCE)認証基盤(`connectors/x_oauth2.py`・S1 = GH#58)

ブックマーク取得(`/2/users/:id/bookmarks`)は OAuth 2.0 user context が API 上の必須のため、S1 の取込(ブックマーク・いいね)は **OAuth2 に統一**する(ADR 0001「X ソースと認証」。認証経路を1本に保つ実装選択)。ホームTL・検索の OAuth 1.0a(`XApiSession`)とは独立の並走。

- **前提事実(API 実確認 2026-07-13)**: access token は **offline.access を付けても 2h で失効**する。無人朝バッチには refresh token(**offline.access scope 必須** — Issue #58 記載の 4 scope に追加)が要る。refresh token は**ローテーションされる**(使用のたびに新 refresh token が発行される)前提で設計する。
- **scope**: `tweet.read users.read bookmark.read like.read offline.access`(S1b の取込に必要な最小集合)。
- **認可フロー(`intel x-auth`)**: PKCE(S256)+ loopback redirect(`http://127.0.0.1:<port>/callback`。port は config `[x_oauth2].callback_port`、既定 8791。**portal 登録の redirect URI と完全一致必須**)。state 照合(不一致は中断)。public client(Native App 型)は body の client_id、confidential client(Web App 型)は .env `X_OAUTH2_CLIENT_SECRET` の Basic 認証。callback を受けたあとの code の交換から token の保存までは、refresh と同じプロセス間のファイルロック(config_dir の `x-oauth2-token.lock`)の中で行う。朝バッチ・doctor の refresh の往復中に再認可しても、後から保存される refresh の結果で再認可の token を上書きさせないため。ブラウザ待ちの間はロックを持たない(GH#233)。
- **token 保存**: `~/.config/intel/x-oauth2-token.json`(chmod 600・.env と同じ扱い = 設計 05 §6)。**0600 一時ファイル → rename の原子的更新**(ローテーションされた refresh token の取りこぼし = 認可チェーン切断を防ぐ)。壊れたファイルは fail fast(黙って再認可に倒さない)。
- **セッション(`XOAuth2Session`)**: Bearer つき GET。共通リトライ規約(設計 05 §8)は `XApiSession` と同形(429 は待たず即返す)。失効 5 分前の先回り refresh + 401 時の1回 refresh。**新 token は使用前に永続化**(保存失敗時に旧 token で続行しない)。refresh 失敗は再認可(`intel x-auth`)の案内つきエラー。token エンドポイントへの通信の失敗も、同じ分類済みのエラーにする(取込の kind 単位の隔離を抜けさせない。要求は資格情報を持つので、元の例外はつながず型の名前だけを残す。GH#330)。
- **検証**: `/2/users/me`(読取課金なし)。x-auth の完了確認と doctor #13 が使う。

### 興味シグナル取込(`connectors/x_signals.py`・S1b = GH#58)

自分の X 行動(🔖ブックマーク・❤️いいね・🔁自RT)を**探索シグナル**として `x_signals` に記録する(ADR 0001 の興味フィードバックループ入力)。like / self_rt は items に書かない(§1 の collect 契約から独立。朝バッチの専用段 `x_signals` として実行)。**🔖ブックマークだけは保存アイテムとして items にも登録する**(GH#99):

- **item 化(bookmark のみ)**: tweet.fields に `note_tweet`(全文。GH#94 と同じ理由)と `entities`(外部リンク)を追加(同一読取 = 課金不変)。外部リンク付き(X のホストの別名(設計 01 §4 の手順 3 と同じ集合)と t.co 等の内部リンクは除外。GH#232。分解できないリンクも候補から外す。GH#329)は**先頭リンクを canonical_url に**(記事本体を恒久保存対象へ。本文は extract 段が取得するため raw_content は持たない)。リンクなしは**ツイート URL を canonical_url に**、全文を raw_content に(type='x' の TTL 規約に従う)。canonical / (source, external_id) の既存行・dedup 親があれば新規 item を作らずそこへ紐付ける(`x_signals.item_id`)
- **帰属と境界**: 帰属先は enabled=0 の sources 行 **`x:bookmark`**(config `{"mode": "bookmark"}`。コードが自動作成)。collect の走査対象外で、XRouter に来たら fail fast。doctor #5 は enabled 化を WARN。**digest ランキング・全滅判定・トレンドには参加しない**(must-include の別枠。除外は ranker / builder / trend 側)。事前選別は blocklist のみ適用(stale / x_noise / daily_cap は本人意思を優先して適用外 = manual と同じ理屈。件数は日次 cap / --backfill --limit で有界)。**ブックマーク属性の判定は `x_signals.item_id` の紐付けで行う**(items の帰属ソースでは判定しない。RSS 等の既存 item へ合流したケースでも属性が失われないため)。合流先が前日以前に stale / x_noise / daily_cap で `skipped` になっていた item なら、紐付けと同時に `status='new'`(`skip_reason` は NULL)へ戻して事前選別をやり直させる(戻さないと分析にも 🔖 保存節にも載らない。GH#176)。blocklist による `skipped` は戻さない

- **エンドポイントと認証**: `/2/users/:id/bookmarks`・`/2/users/:id/liked_tweets`・自RT の `/2/users/:id/tweets`(`exclude=replies` + referenced_tweets の `type='retweeted'` で判定)。**3 種とも OAuth 2.0(`XOAuth2Session`)に統一**(ADR 0001「認証経路を1本に保つ」。自RT は OAuth1 でも取れるが S1a の OAuth2 に相乗り)。me_id は users/me(読取課金なし)で 1 回だけ取得し全 kind で共用
- **差分検出**: bookmarks / liked_tweets は since_id 非対応のため、新→旧に読み**既知 tweet_id 到達で打ち切り**(`UNIQUE (kind, tweet_id)` + INSERT OR IGNORE の rowcount で判定)。解除→再登録で古い側に隠れた新規は追わない(見逃し防止は非目標。ホームTL と同方針)。**自RT は since_id カーソル**(`x_signal_cursors`): 通常投稿は保存されず既知到達方式が効かない(毎回同じ先頭ページを再課金する)ため。カーソル規約はホームTL と同じ「**打ち切り時も今回の最大 id へ進める**」(古い側は追わない)。ページの挿入は明示トランザクション(BEGIN IMMEDIATE)で原子化(接続は autocommit。半端なページが残ると次回の既知到達が未保存の残りを飛ばす)
- **自RT の正規化**: 記録する id・著者・本文・時刻は **RT 元ツイートのもの**(興味の対象は元ツイ。ホームTL の RT 正規化と同方針)。通常ポスト・引用はシグナル対象外
- **予算**: `[x_signals].daily_read_cap`(**既定 0 = 取込無効**。1〜4 は全 kind が黙って skip になるため ConfigError)。読取 = 取得ツイート数(collect と同じ意味論)。上限は **JST の暦日の上限で、同じ日の実行で共有する**: 台帳 `x_signal_runs.api_reads` の、同じ日の通常の取込の行(`backfill = 0`)の合計(当日は `started_at` を JST の暦日に直して決める。失敗の行も数える)を引いた残りから、下の順で消費する(GH#178。朝バッチの再走・単独の `intel x-signals` で上限を超えない)。合算 `[x.combined_daily_read_cap]` の検証に含まれ、有効化は内訳の再配分 = 人間の config 変更(ADR 0001。例: home 115 / search 35 / signals 15)。予算は bookmark → like → self_rt の順(強シグナル優先)に消費し、残 < 5(liked_tweets / user tweets の max_results 下限)で打ち切り。差分取得(backfill でない取込)では**各 kind の 1 ページ目を最小ページ(5 件)で要求**し、既知到達(自RT は since_id の境界)で止まれば残りを次の kind に回す(読取は既知の判定より先にページ全件ぶん計上されるため、残予算の全量を要求すると、既知のブックマークが上限以上ある日に読み直しだけで予算を使い切り、like / self_rt が毎日 skip される。GH#174)。打ち切り条件(既知到達など)に当たらず次のページがあるときは、2 ページ目から残予算と 100 件の小さい方まで広げる(新着が続く日は強シグナル優先のまま前の kind が予算を使う)。backfill は従来どおり 1 ページ目から残予算(≤ 100)を要求する。**途中ページ失敗でも取得済みぶんは必ず予算・台帳に計上**(過少計上で後続 kind に予算が再配分され cap を実質超過しない)。429 は待たず打ち切り(取得 0 のままの 429 はその kind の失敗として記録)
- **cold start**: 日次 run でなく **`intel x-signals --backfill --limit N` の手動実行**(課金操作は人間ゲート = ADR 0001 の原則)。`--kind` で対象を限定できる(過去ブックマークの item 化 backfill 等、読取を 1 kind に絞る。GH#99)。「kind ごと直近 N 件(既定 100)を種に」の上限つきで、最悪コスト = 3 × N × 単価に有界(全ページ辿りは過去のいいねが多いとコスト事故になるため。GH#84)。自RT も since_id カーソルを送らずに直近 N 件を読む(カーソルがあっても、それより古い過去分を補える)。取得後のカーソルは、今回の最大 id が保存時の値より大きいときだけ進め、後ろへは戻さない(取得中に朝バッチなど別の実行が進めた値も戻さない。GH#231)。backfill 分の observed_at は一括時刻になるため直近性の代理としては使えない(制約として許容)。backfill は日次の上限の外の別枠(GH#178): 台帳を見ずに `--limit` まで読み(同じ日に通常の取込が上限を使い切っていても読む)、その行は `x_signal_runs.backfill = 1`(migration 023)で記録して、通常の取込の当日の使用量に数えない(`--kind bookmark` の backfill で、同じ日の like / self_rt の取込が止まらない)。費用の集計(doctor #9)には入るので、backfill をした日の読取は日次の上限に backfill のぶんが足される(人が決めた追加の支出)
- **障害隔離**: kind 単位で隔離(bookmark の 403 = scope 不備でも like / self_rt は取る)。kind ごとの成否を判定し(0 件は正常な成功・**取得 0 のままの rate limit は失敗**)、**試行した全 kind が失敗したときは段の失敗として loud に落とす**(GH#74 と同方針)。cap > 0 なのに未認可・client_id 欠落も段の失敗(黙って skip しない)
- **記録**: 取込 1 回ごとに `x_signal_runs` へ 1 行(api_reads・estimated_cost_usd・new_signals・backfill。設計 01 §7・§9)。doctor #9 の当月コストに合算。kind の隔離の外の例外(保存の DB エラー・想定外のバグなど)で段が落ちるときも、送出の前にそれまでの読取数と費用を `failed` の行で残す(日次の上限の当日の使用量から漏らさない。台帳を書けなくても元の例外を送出する。GH#330)

### Day 0 テストスクリプト仕様(`scripts/day0_x_probe.py`)

本体外の使い捨てスクリプト(vibe 品質でよい)。X コネクタと同じ認証・エンドポイントを使い、以下を実測して合否を出す。

| 測定 | 方法 | 合格基準 |
|---|---|---|
| 取得範囲・時系列性 | max_results=100 × 最大5ページ取得し、created_at の範囲と単調性を表示。author 別件数と全件 CSV を出力し、直近24hのTLと目視突合 | 直近24hのフォロー投稿が時系列で取れ、体感欠落1割未満 |
| 実効単価 | 実行前後の課金メーター/クレジット残を手動記録(スクリプトは総読取件数を表示) | 実効単価 × daily_read_cap × 30日 ≦ $20(この `daily_read_cap` はホームTL単独の Day 0 測定基準。運用の予算はホームTL + 検索の**合算日次上限 ≦ 165件/日**で管理する。ADR 0001「予算配分」) |
| rate limit | 各レスポンスの x-rate-limit-remaining / reset を表示 | 1回のバッチで daily_read_cap 分を取得完了できる |
| ノイズ率 | author 別集計 CSV を目視で娯楽/追跡に分類 | 娯楽混入が過半なら追跡専用アカウント新設に切替 |
| リスト方式の再検討材料 | 厳選リスト(既存 or 仮作成)で `GET /2/lists/{id}/tweets` を1ページ取得し、取得可否と課金カウント(実効単価)を小確認 | ホームTLと単価が同等なら「リストは5倍高い」前提は消滅。ホームTL継続 / 厳選リスト / 追跡専用アカウントのいずれかを決定して記録(plan #1 の完了条件) |

CSV に出す API 由来の文字列(本文・ユーザー名・id・時刻)は、先頭が `=` `+` `-` `@`・タブ・CR なら先頭に `'` を付けて出す。表計算ソフトで開いたときに数式として解釈させないため(GH#246)。数値の列(is_rt・is_reply・count)はそのまま出す。

不合格時は PROPOSAL のゲート不通過フロー(RSS+手動URLに縮小 or リスト方式再検討)へ。

## 3. RSS コネクタ(`connectors/rss.py`)

- **取得**: httpx で GET し、レスポンスボディを feedparser に渡す(feedparser 内蔵HTTPは使わない。理由: timeout・UA・エラー分類・条件付きGETを全コネクタで httpx に統一)。timeout とリトライは設計 05 §8 の共通規約(connect 10s / read 30s)。
- **相対リンク(GH#185)**: 相対の記事リンクは、取得した URL(リダイレクト後の最終 URL)を基準に絶対 URL へ解決する。`xml:base` があればそちらを優先する。解決は feedparser に任せ、基準は content-location として渡す(文字コードの推定は変えない)。同じ基準で、本文(content / summary)の中の相対リンクと、URI として扱う id(`isPermaLink="false"` でない guid など)も絶対化される。基準と合わせて分解できない記事リンクは feedparser が空にするので、その entry は取り込まない(link の無い entry と同じ扱い。error 行も残らない)。
- **条件付き GET**: `cursor = {"etag": str|None, "last_modified": str|None}` を保存し、`If-None-Match` / `If-Modified-Since` を送る。304 は正常 no-op(collect_runs に `status='ok'`・`items_fetched=0` で記録。error 列には書かない正常系。01 の status CHECK に値を増やさない)。
- **重複排除キー**: entry の `id`(guid)を external_id に。無ければ正規化 URL の hash にフォールバック。
- **raw_content**: `content` / `summary` があれば RawItem.raw_content に同梱。全文フィードなら本文抽出の HTTP を節約できる(使うかは extraction 層が判断)。
- **フィード死活**: consecutive_failures ≥ 7(約1週間)で doctor #14 が「死亡疑い」警告(watchdog は通知しない)。410 Gone と恒久リダイレクトループは即 FatalError。自動削除・自動 disable はせず週次セルフレビューで棚卸し。理由: RSS は一時障害が多く、自動判定の誤爆コストの方が高い。
- **週 1 回の健康診断(GH#150)**: 死活の判定(上)と移転(下)は機械が見つけるが、止める・URL を直す判断は人が行う。その判断の合図として、poller の内蔵タイマーが毎週日曜の夜アンケートの時刻に 1 通送る(`app/source_health.py`。設計 05 §4)。中身は、連続で失敗しているソース(1 回以上・回数と直近のエラー)、移転したソースと移転先、14 日間新しい記事が入っていないソース(登録して 14 日未満は除く)、直近 4 週の 👍・🔖 の率が低いソース(配信 10 件以上・低い順に 3 つ。KPI 04 をソース単位にしたもの)。X を止めている間は X のソースを見ない。何も無い週も送る(棚卸しの合図を兼ねる)。自動で止めたり書き換えたりはしない
- **恒久リダイレクト(301 / 308)**: 取得は転送先から続け、ログに WARNING を出し、移転先(リダイレクトを辿った最終の URL)を `sources.moved_url`(migration 016)に残す。移転なしで取れた回に消え、取れなかった回は変えない。週次のソースの健康診断(GH#150)で知らせる。sources.config の URL 書き換えは人が行う(sources はユーザー管理データのため。自動では書き換えない)。

## 4. 手動 URL コネクタ(Telegram 投入フロー)

手動URLは **Connector を実装しない**。受信デーモン(`delivery/poller.py`)が直接 items に書く。理由: 「巡回して取る」モデルに合わないものを fetch() に無理に対称化しない。

フロー:

1. 受信デーモンが message を受信 → **owner allowlist 検証**(不一致は記録のみで破棄。PROPOSAL §Telegram 実装仕様)。
2. owner chat からのリプライは先に振り分け、URL を取り込まない: 深掘りカード・その返事へのリプライ(質問・感想。設計 06 §6)、ダイジェストの記事・ヘッダへのリプライ(感想。設計 04)、未消化一覧へのリプライ(深掘りの指定。設計 06 §2)。それ以外のメッセージから URL を抽出する(http/https のみ。複数あれば各々投入。Telegram の entities を優先し、URL の直後の句読点・対になっていない閉じ括弧は含めない。設計 04 §5)。URL が無ければ案内文(URL を送ると翌朝のダイジェスト候補になる)を返す。夜アンケートの回答はボタン(callback)で受けるので、メッセージの経路には来ない。
3. **canonical_url のみ**で既存照会。既存なら「投入済みです」と返信して終了(冪等)。content_hash は照会に使わない(manual は本文が無く正規化テキストが空になるため、空ハッシュ一致で別 URL まで duplicate 化する。01 §5〜6)。
4. 新規なら `items` に source=manual(sources の固定1行)・`status='new'`(01 の状態語彙に従う。専用の pending 状態は持たない)で INSERT し、「受け付けた。明朝のダイジェストで分析される」と即返信。
5. 本文抽出・分析は**翌朝バッチに乗せる**(即時分析しない。理由: LLM 呼び出し経路を朝バッチ1本に保ち、クォータ・コスト・障害面を増やさない。急ぎで読むなら人がそのまま開けばよい)。

拡張点: 「即時要約して返す」モードはフェーズ2候補(受信デーモンから LLM を呼ぶ経路が増えるため MVP では見送り)。

## 5. 他コンポーネントとの境界

- コネクタ → collect: RawItem のみ渡す。items への正規化・INSERT・カーソル保存は collect。
- collect → extraction: 本文取得・trafilatura は extraction の仕事。コネクタは raw_content のヒント渡しまで。
- collect_runs / sources のスキーマ正本: 01(DB設計)。doctor の表示仕様: 運用設計文書(05)。
