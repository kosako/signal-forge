# 設計 02: コネクタ(X / RSS / 手動URL)

ステータス: Draft v1(2026-07-05)/ 正本の上位文書: `docs/PROPOSAL.md`

対象: `app/connectors/` と `intel collect`。データモデルの正本は 01(DB設計)、本文抽出は extraction/ の管轄(コネクタは本文抽出をしない)。

## 1. 共通 Connector インターフェース

```python
# app/connectors/base.py
@dataclass(frozen=True)
class RawItem:
    external_id: str          # ソース内の自然キー(tweet id / RSS guid / URL)
    url: str                  # 正規化前URL。正規化は collect 側で一元化
    title: str | None
    author: str | None
    published_at: datetime | None   # UTC
    raw_content: str | None   # ソースが本文を同梱する場合のみ(RSS content 等)。抽出層へのヒント。
                              #   collect が items.raw_payload(01 §6 の JSON 形・サイズ上限あり)に
                              #   保存し、extraction は DB 経由で参照する(直接受け渡ししない)
    meta: dict                # ソース固有情報(retweet種別など)。JSON保存

@dataclass(frozen=True)
class FetchResult:
    items: list[RawItem]
    next_cursor: dict         # sources.cursor に JSON 保存する不透明な辞書
    stats: FetchStats         # items_fetched, api_reads(collect_runs へ記録)+ http_status, duration_ms(ログのみ)

class Connector(Protocol):
    source_type: str          # "x" | "rss"
    def fetch(self, source_row: SourceRow, cursor: dict) -> FetchResult: ...
```

- **fetch は読み取り専用・副作用なし**(DB に触らない)。items への書き込み・URL正規化・重複排除・カーソル保存はすべて `intel collect` が行う。理由: 冪等性の担保箇所を1箇所に集約し、コネクタ追加時にバグ面を増やさない。
- **冪等性契約**: 同じ cursor で fetch を何度呼んでも、collect 後の items テーブルは同一状態になること。担保は items の UNIQUE 制約(`source_id + external_id` と `canonical_url`)+ `INSERT OR IGNORE`(内容ベースの 24h dedup は 01 §6。正規化テキストが空/極端に短いアイテムは対象外)。コネクタ側は重複を返してよい。
- **カーソル更新のタイミング**: そのソースの全 RawItem の INSERT が成功した後、同一トランザクションで `sources.cursor` を更新。途中クラッシュ時はカーソル未更新→次回重複取得→UNIQUE で吸収。理由: 「カーソルだけ進んで items が欠ける」取りこぼしを構造的に排除(欠落方向より重複方向に倒す)。
- **エラー分類(全コネクタ共通)**:
  - `RetryableError`(接続失敗・timeout・5xx・429): リトライは**設計 05 §8 の共通規約に従う**(2回・指数バックオフ 1s→4s + jitter。429 は Retry-After 優先)。リトライ尽きでそのソースを当日スキップ。
  - `FatalError`(401/403・設定不備): リトライせずスキップ、doctor 警告対象。
  - いずれもソース単位で握りつぶし、**他ソースの collect は必ず継続**(縮退の基本形。X が死んでも RSS+手動URLでダイジェストは出る)。
- **記録**: fetch 1回ごとに `collect_runs`(source_id, started_at, finished_at, status, items_fetched, api_reads, estimated_cost_usd, error)へ1行。テーブル定義の正本は 01(語彙も 01 に従う)。http_status・duration_ms はログ(JSON Lines)のみで DB には持たない(1人運用のデバッグはログで足りる)。`sources` には last_success_at / last_error_at / consecutive_failures を反映する(成功で 0 リセット・失敗で +1。01 §3)。
- **SSRF ガード**: 短縮 URL 展開・本文抽出を含む外部 URL fetch の全経路は、設計 05 §8 の共通 HTTP クライアント規約(global でないアドレスへの接続拒否 = is_global allowlist 方式、リダイレクト各ホップで再検査、検証済み IP へのピン留め)に従う。
- 拡張点: 新ソースは Connector 実装+sources 1行の追加のみ。スケジューリング分岐(ソース別頻度)は現時点で作らない(朝バッチ1回で十分)。

## 2. X コネクタ(`connectors/x.py`)

- **エンドポイント**: `GET /2/timelines/reverse_chronological`(逆時系列ホームTL)のみ。パラメータ: `max_results=100`, `since_id`, `pagination_token`, `tweet.fields=created_at,author_id,entities`, `expansions=author_id`。単価は Day 0 実測(Posts Read $0.005/件の可能性が高い。PROPOSAL §Day 0 ゲート)。
- **カーソル**: `cursor = {"since_id": str}`。ページングは `pagination_token` で新→旧に辿り、(a) since_id に到達、(b) **日次取得上限**(config `x.daily_read_cap`、コスト逆算値。$0.005 想定なら 130)到達、(c) rate limit 残なし、のいずれかで打ち切る。打ち切りで捨てるのは古い側であり、見逃し防止は非目標なので許容。次回 since_id は**今回取得した最大 tweet id**(打ち切り時も同じ。古い側の穴を追わない)。
- **rate limit**: 期待値 5 req/15min。`x-rate-limit-remaining` を見て残0なら打ち切り(15分待ちはしない。朝バッチ1回で取り切れる量に cap を設計する前提)。
- **コスト記録**: 取得件数 × config `x.unit_price_usd`(Day 0 実測値を設定)を collect_runs.estimated_cost_usd に記録。doctor が月次見込みを集計し、超過ペースで警告。
- **リツイート/リプライ**: RT は referenced tweet の URL に正規化して重複排除に乗せる。自分宛以外のリプライは既定で除外(meta に記録)。理由: ノイズ率対策の最安の一手。
- **本文保持ポリシー(RSS と分離)**: tweet 本文は分析に必要なので documents に入れるが **TTL 付きキャッシュのみ・恒久保存しない**(恒久保存は自作要約+メタ+URL)。**確定済み(2026-07-06、Issue #2)**: `x_cache_ttl_days=7`、X 由来の抜粋は Obsidian に置かない(詳細は 01 §6 / 04 §7)。
- **縮退**: consecutive_failures ≥ 3 で doctor・watchdog 通知(自動 disable はしない。理由: 勝手に情報源が消えるより通知して人が `sources.enabled=0` を切る方が安全)。X 停止中もパイプラインはソース非依存で RSS+手動URLで継続。

### Day 0 テストスクリプト仕様(`scripts/day0_x_probe.py`)

本体外の使い捨てスクリプト(vibe 品質でよい)。X コネクタと同じ認証・エンドポイントを使い、以下を実測して合否を出す。

| 測定 | 方法 | 合格基準 |
|---|---|---|
| 取得範囲・時系列性 | max_results=100 × 最大5ページ取得し、created_at の範囲と単調性を表示。author 別件数と全件 CSV を出力し、直近24hのTLと目視突合 | 直近24hのフォロー投稿が時系列で取れ、体感欠落1割未満 |
| 実効単価 | 実行前後の課金メーター/クレジット残を手動記録(スクリプトは総読取件数を表示) | 実効単価 × daily_read_cap × 30日 ≦ $20 |
| rate limit | 各レスポンスの x-rate-limit-remaining / reset を表示 | 1回のバッチで daily_read_cap 分を取得完了できる |
| ノイズ率 | author 別集計 CSV を目視で娯楽/追跡に分類 | 娯楽混入が過半なら追跡専用アカウント新設に切替 |
| リスト方式の再検討材料 | 厳選リスト(既存 or 仮作成)で `GET /2/lists/{id}/tweets` を1ページ取得し、取得可否と課金カウント(実効単価)を小確認 | ホームTLと単価が同等なら「リストは5倍高い」前提は消滅。ホームTL継続 / 厳選リスト / 追跡専用アカウントのいずれかを決定して記録(plan #1 の完了条件) |

不合格時は PROPOSAL のゲート不通過フロー(RSS+手動URLに縮小 or リスト方式再検討)へ。

## 3. RSS コネクタ(`connectors/rss.py`)

- **取得**: httpx で GET し、レスポンスボディを feedparser に渡す(feedparser 内蔵HTTPは使わない。理由: timeout・UA・エラー分類・条件付きGETを全コネクタで httpx に統一)。timeout 15s。
- **条件付き GET**: `cursor = {"etag": str|None, "last_modified": str|None}` を保存し、`If-None-Match` / `If-Modified-Since` を送る。304 は正常 no-op(collect_runs に `status='ok'`・`items_fetched=0` で記録。error 列には書かない正常系。01 の status CHECK に値を増やさない)。
- **重複排除キー**: entry の `id`(guid)を external_id に。無ければ正規化 URL の hash にフォールバック。
- **raw_content**: `content` / `summary` があれば RawItem.raw_content に同梱。全文フィードなら本文抽出の HTTP を節約できる(使うかは extraction 層が判断)。
- **フィード死活**: consecutive_failures ≥ 7(約1週間)で doctor が「死亡疑い」警告。410 Gone と恒久リダイレクトループは即 FatalError。自動削除・自動 disable はせず週次セルフレビューで棚卸し。理由: RSS は一時障害が多く、自動判定の誤爆コストの方が高い。
- **恒久リダイレクト(301)**: 警告表示のみ。sources.config の URL 書き換えは人が行う(sources はユーザー管理データのため)。

## 4. 手動 URL コネクタ(Telegram 投入フロー)

手動URLは **Connector を実装しない**。受信デーモン(delivery/telegram.py 側)が直接 items に書く。理由: 「巡回して取る」モデルに合わないものを fetch() に無理に対称化しない。

フロー:

1. 受信デーモンが message を受信 → **owner allowlist 検証**(不一致は記録のみで破棄。PROPOSAL §Telegram 実装仕様)。
2. テキストから URL を抽出(http/https のみ。複数あれば各々投入)。URL が無い message は夜アンケート等の他ハンドラへ。
3. **canonical_url のみ**で既存照会。既存なら「取り込み済み」と返信して終了(冪等)。content_hash は照会に使わない(manual は本文が無く正規化テキストが空になるため、空ハッシュ一致で別 URL まで duplicate 化する。01 §5〜6)。
4. 新規なら `items` に source=manual(sources の固定1行)・`status='new'`(01 の状態語彙に従う。専用の pending 状態は持たない)で INSERT し、「受け付けた。明朝のダイジェストで分析される」と即返信。
5. 本文抽出・分析は**翌朝バッチに乗せる**(即時分析しない。理由: LLM 呼び出し経路を朝バッチ1本に保ち、クォータ・コスト・障害面を増やさない。急ぎで読むなら人がそのまま開けばよい)。

拡張点: 「即時要約して返す」モードはフェーズ2候補(受信デーモンから LLM を呼ぶ経路が増えるため MVP では見送り)。

## 5. 他コンポーネントとの境界

- コネクタ → collect: RawItem のみ渡す。items への正規化・INSERT・カーソル保存は collect。
- collect → extraction: 本文取得・trafilatura は extraction の仕事。コネクタは raw_content のヒント渡しまで。
- collect_runs / sources のスキーマ正本: 01(DB設計)。doctor の表示仕様: 運用設計文書(05)。
