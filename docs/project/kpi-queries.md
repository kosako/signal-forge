# KPI 集計クエリ集(plan #24)

定義の正本: [plan.md](plan.md) §6(週次レビューの型)/ PROPOSAL §成功条件と KPI 測定定義。
クエリ本体は [../../scripts/kpi/](../../scripts/kpi/) の `.sql`(本書は索引と使い方)。

## 使い方

```sh
sqlite3 -header -column ~/.local/share/intel/intel.db < scripts/kpi/01-daily-kpi.sql
```

(`[storage].db_path` を設定している場合はそのパスに読み替える)

## 索引

| # | ファイル | 出すもの | 週次レビューの項目 |
|---|---------|---------|------------------|
| 01 | `01-daily-kpi.sql` | 日次 KPI(👍∨🔖≥3)・反応率・無効日判定 | §6-1, 2 |
| 02 | `02-patrol-replacement.sql` | 巡回代替日(質問1の最新回答) | §6-3 |
| 03 | `03-read-minutes.sql` | 読了時間の分布(質問2) | §6-4 |
| 04 | `04-source-category-rates.sql` | ソース別・カテゴリ別 👍∨🔖 率 | §6-5 |
| 05 | `05-spot-check.sql` | 👍校正スポットチェック(7日前の👍から無作為3件) | §6-7 |
| 06 | `06-phase2-boundary.sql` | 🔖累計・深掘り実施率・フェーズ2境界判定 | 最終判定 |
| 07 | `07-invalid-streak.sql` | 無効日3日連続の検知 | R1 発動判定 |
| 08 | `08-weekly-summary.sql` | 直近7日の配信日数・KPI 達成日数・無効日数・fail-closed 週判定 | §6-1〜2 |
| 09 | (SQL でなく)`uv run intel x-insights` | X 興味シグナルのインサイト(常連著者・頻出テーマ・直近シグナル)。重み係数は config `[x_signals]`(GH#58 S1c) | §6-8 |

## fail-closed の突合(SQL に出ない分の手作業)

- 反応の KPI(01/07/08)は**配信済み digest を母集団**にしており、反応ゼロの配信日も
  反応率 0% の無効日として SQL 内で判定される(手動補正は不要)。
- **夜アンケート未回答の日**は 02 に現れない。暦日と突き合わせ、未回答日は
  **代替できなかった日**として数える(PROPOSAL の fail-closed 定義)。

## コスト

コスト集計は SQL でなく `intel doctor --cost`(当月実績と着地見込み)を使う。
