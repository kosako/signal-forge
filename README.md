# signal-forge

X・RSS・手動で投げた URL を、毎朝 10 件以内の日本語ダイジェストにまとめる個人用のインテリジェンス基盤。受け取りは Telegram、蓄積は Obsidian。フルスクラッチだが徹底的に最小の構成(A-lite)で、自宅の Mac の launchd で無人で動かす。

解く痛みは 2 つ。情報収集の巡回(約 1 時間/日、主に X)にかかる時間を縮めることと、「後で読む」の未読負債をなくすこと。見逃し防止は目的にしない。企画の詳細は [docs/PROPOSAL.md](docs/PROPOSAL.md)。

## 今の状態

MVP(A-lite)は稼働中。フェーズ 2 として、気になった記事を AI が調べてノートにする「深掘り」を実装している。

進捗の正本は GitHub Issues と repo の外のナレッジツールにあり、この README には書かない。

## しくみ

```text
 X API(任意) / RSS
        │
        ▼
 朝バッチ  intel run(launchd。既定 06:30 起動)
   collect → x_signals → extract → analyze → digest → export → deliver → dig → purge → backup
                                                        │         │        │
                                                        ▼         ▼        ▼
                                                  Obsidian    Telegram   DeepDive ノート(vault)
                                                  vault      (ダイジェスト) + 概要カード(Telegram)
        ▲
        │ 読み書き(2 つのプロセスは SQLite を介してだけやり取りする)
        ▼
 SQLite(正本)
        ▲
        │ 読み書き
        ▼
 受信デーモン  intel poll(launchd KeepAlive で常駐。Telegram の long polling)
   受ける: ボタン / URL / ダイジェストへの感想 / 夜の回答と深掘りの指定 / 深掘りカードへの質問
   内蔵タイマー: 夜アンケート(既定 21:30)/ watchdog(既定 07:15)
   質問 → intel answer(別プロセス)→ Telegram のリプライで返事
```

- **朝バッチ `intel run`**: 上の段を順に実行して終わる。段の順序の正本は [設計 00 §2](docs/design/00-architecture.md)。
  - x_signals は `[x_signals].daily_read_cap > 0`、dig は `[dig].daily_cap > 0` のときだけ動く。deliver は Telegram が完全に未設定(`owner_chat_id` も bot token も無い)なら WARN を出して飛ばす。片方だけの設定は設定ミスとして deliver 段が失敗する。
  - 段の失敗はその段で止め、後の段は続ける。失敗した段があれば(Telegram が完全に未設定でなければ)Telegram に知らせ、exit 1 で終わる。
  - export を deliver より前に置くので、Telegram が失敗しても Obsidian への蓄積は済む。
  - 冪等なので再実行してよい。配信済みのダイジェストは送り直さない(`--force` を付けたときを除く)。
- **受信デーモン `intel poll`**: Telegram から次のものを受けて DB に記録する。
  - 反応ボタン(👍 価値あり / 🔖 深掘り / ✅ 既知 / 👎 不要)。
  - URL のメッセージ。手動投入として items に入り、翌朝のダイジェストで分析される。
  - ダイジェストの記事・ヘッダへのリプライ(感想)。翌朝の export で Obsidian の Items / Daily ノートに反映する。
  - 夜アンケートの回答と、未消化一覧へのリプライ(深掘りの指定)。
  - 深掘りカードとその返事へのリプライ。感想として記録し、1 日の上限(`[dig].answer_daily_cap`)の内なら質問として返事を作る。
  - 内蔵タイマーで夜アンケートと watchdog を送る。時刻は config の `[telegram].survey_time` / `watchdog_time` で変えられる(上の時刻は既定値)。watchdog は、当日のダイジェストが未生成・未配信のときと、X ソースが続けて失敗しているとき(X を有効にしている場合。[設計 02 §2](docs/design/02-connectors.md))に知らせる。
  - `[telegram].owner_user_id` 以外からの update は、ログに WARN を 1 行残すだけで破棄する(本文は保存せず、応答も返さない)。
- **深掘りの流れ**([設計 06](docs/design/06-deepdive-experience.md))
  1. 夜: 未消化一覧(Telegram の 🔖 と X のブックマーク)に自由文でリプライし、「番号 + 観点」か「パス」を指定する。X で新しくブックマークした投稿(x_signals 段が取り込んだもの)は、指定しなくてもキューに入る。Telegram の 🔖 ボタンは未消化一覧に載るだけで、キューには入らない。
  2. 朝: dig 段が 1 日の上限(既定 2 件)まで調べ、vault の `SignalForge/DeepDive/` に解説ノートと、原文ノート(日本語のソース)か対訳ノート(それ以外)を書く(ツイート本文だけのものは解説ノートのみ)。
  3. 概要カードを Telegram に送る。カードのボタンで、元記事を開く・元記事の要約(英語なら和訳)・もう少し詳しい説明を頼める。
  4. カードへのリプライ: poller が台帳に積み、`intel answer` を別プロセスで起動して返事をリプライで送る(1 日の上限は既定 20 件)。LLM が結論・感想(take)と分けたリプライは、原文を解説ノートの末尾に足す(ノートは書き直さず追記だけ)。
- **データ**: SQLite が正本で、Obsidian は書き出し先。Daily / Items ノートの生成部分は SQLite のデータから export が書く(ノートに足した手書きメモは vault にしか無い)。DeepDive ノートだけは書き出した後に本文を DB に残さず、vault 側が恒久の置き場になる([設計 01 §7](docs/design/01-data-model.md)、[ADR 0004](docs/adr/0004-deepdive-in-agent-obsidian-substrate.md))。vault は intel のバックアップの対象外(runbook §7)なので、守るなら別に手当てする。
- **LLM**: 使い捨ての `codex exec` を、ツールなし・ネットワーク遮断などで隔離して呼ぶ。今実装しているエンジンは `codex-cli` だけ([設計 00 §5](docs/design/00-architecture.md))。

## 必要なもの

- macOS と launchd。システムのタイムゾーンが JST であること。
- Python 3.12 以上と uv。
- Telegram の bot(BotFather で作る)。
- codex CLI。`codex login` を済ませておく。呼び出しごとの追加の支払いは無い(codex のサブスク枠を使う)が、LLM の呼び出しはその枠(クレジット)を消費する。
- Obsidian の vault(書き出し先のローカルフォルダ)。
- X API は任意で、既定は無効(`[x].enabled = false`)。無効でも RSS と手動 URL だけで全段が動く。使うなら公式 API の従量課金がかかる。
- 実行環境(とくに launchd)に `ANTHROPIC_API_KEY` を置かない。`intel doctor` の #12 は、doctor を実行した環境にこのキーがあると WARN を出す(launchd 側は plist の雛形が PATH と HOME だけを渡す)。

## はじめかた(初回)

以下の手順はすべて clone したリポジトリのルートで実行する。

1. 依存を入れる。

   ```sh
   uv sync
   ```

2. `~/.config/intel/config.toml` を作る。未設定のキーは既定値になり、不明なセクションやキーを書くと読み込みが止まる。最低限、次を設定する([app/config.py](app/config.py) に全キーと既定値がある)。

   ```toml
   [telegram]
   owner_user_id = <自分の Telegram user_id>
   owner_chat_id = <bot との DM の chat_id>

   [obsidian]
   vault_path = "~/<vault へのパス>"   # 出力は <vault>/SignalForge/ の下
   ```

   - `owner_user_id` は受け付ける相手(これ以外からの update は破棄)、`owner_chat_id` は配信・通知の送り先(bot との DM)。未設定だと doctor #4 が FAIL になる。
   - ID を調べるには、BotFather で発行した bot token(手順 3 で `.env` に置くもの)を使う。poller を起動する前に bot へ DM を 1 通送り、ブラウザか curl で `https://api.telegram.org/bot<token>/getUpdates` を開いて、応答の `message.from.id`(user_id)と `message.chat.id`(chat_id)を見る。token を含む URL はブラウザやシェルの履歴に残るので、確かめ終えたら履歴から消す(気になる場合は runbook §8 の手順で token を再発行する)。

3. `~/.config/intel/.env` を作り、`chmod 600` にする(group/other に開いていると読み込みを拒否してエラー(ConfigError)で止まり、`.env` を読むコマンド(run・poll・deliver など)が動かない。doctor では #2 が FAIL)。`KEY=VALUE` 形式で、置くキーは次のとおり。

   - `TELEGRAM_BOT_TOKEN`(必須)
   - X を使うときだけ: `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET`。ブックマーク等の取り込み(OAuth 2.0)を Web App 型で使うなら `X_OAUTH2_CLIENT_SECRET`。

4. 関心プロファイルを置く。分析・深掘り・質問への返事はこれを基準にし、無いと analyze・dig・answer が失敗する。

   ```sh
   cp docs/templates/interest-profile.md ~/.config/intel/interest-profile.md
   ```

   中身を自分の関心に書き換える(形式は [設計 03 §4](docs/design/03-analysis-ranking.md))。

5. DB を作る。migration は collect・run・poll などのコマンドが起動時に当てる。`doctor`(フラグなし)・`x-auth`・`x-insights` は当てない。副作用の少ない次のコマンドを 1 回走らせると、DB(既定 `~/.local/share/intel/intel.db`)と表ができる。

   ```sh
   uv run intel doctor --cost   # migration を当て、当月コストを表示するだけ
   ```

6. ソースを登録する。ソースの正本は DB の `sources` 表で、追加と無効化は SQL で行う。RSS は `config` の JSON に `url` を入れる。

   ```sh
   sqlite3 ~/.local/share/intel/intel.db \
     "INSERT INTO sources (type, name, config) VALUES ('rss', '<名前>', '{\"url\": \"<フィードの URL>\"}');"
   ```

   `[storage].db_path` を設定した場合は、そのパスに置き換える。

   - 止めるときは `enabled = 0` にする。手動 URL 用の行は migration が作るので登録しなくてよい。
   - X を使う場合は `[x].enabled = true` と `.env` の 4 キーに加え、sources の行が要る。ホーム TL は `INSERT INTO sources (type, name) VALUES ('x', 'x:home');`(config は NULL のまま)。検索も使うなら `[x_search].slots` を設定し、[設計 02 §2](docs/design/02-connectors.md) の SQL で `{"mode": "search"}` の行を足す。ブックマーク等の取り込みは [ops/runbook.md](ops/runbook.md) の §9・§10。

7. 確かめる。

   ```sh
   uv run intel doctor
   ```

   - FAIL があれば exit 1。#1〜#4・#6・#7 を PASS にする(X を有効にしたときは #5 も、`intel x-auth` を済ませたときは #13 も)。#6 は codex を実際に呼んで隔離を検査する。
   - #6 が `LLMUnavailableError` で FAIL するときは、codex が PATH に無い、`codex login` が済んでいない(`~/.codex/auth.json` が無い)、または codex の版が古く隔離に使う起動引数を受け付けない、のどれか。`command -v codex`、`codex login`、codex の更新の順に確かめる。
   - launchd(#8)は runbook の §2 でロードするまで、バックアップ(#10)は `intel run` の backup 段が 1 回走るまで WARN のままになる。

8. 手動で `uv run intel run` を 1 回流し、ダイジェストが Telegram に届くことを確かめる。続けて `uv run intel poll` を起動し、ボタンを押すと `✓` が付くことを確かめたら Ctrl-C で止める(poll は止めるまで常駐する。launchd の poller と同時に動かすと getUpdates を取り合う)。ダイジェストが「該当なし」(0 件)のときは記事のボタンが無いので、`✓` の確認は記事が載った日に行う(`[telegram].survey_time`(既定 21:30)を過ぎてから poll を起動すると夜アンケートが届くので、そのボタンでも確かめられる)。そのうえで無人運用に移る。手順は [ops/runbook.md](ops/runbook.md) の §1〜§5(plist の生成、bootstrap、pmset wake、Full Disk Access、確認)。

## コマンド

起動は `uv run intel <command>`(リポジトリのルートで)。どのコマンドも `--config-dir`(既定 `~/.config/intel`)で設定の置き場を差し替えられる。定義は [app/cli.py](app/cli.py)。

| コマンド | 役割 | 主なオプション・注意 |
|---|---|---|
| `collect` | 全ソース(X・RSS)から items を取り込む | |
| `extract` | items から本文を抜き出し documents を作る | |
| `analyze` | 事前選別と LLM の本分析で analyses を作る | 関心プロファイルが必要 |
| `digest` | ランキングして日本語ダイジェストを作る | `--force` 生成済みでも当日分を作り直す。配信状態も未配信に戻るので、次の deliver で送り直す |
| `deliver` | 当日のダイジェストを Telegram に配信する | 当日分が無ければ exit 1 |
| `dig` | 深掘りキューの本日分を調べ、DeepDive ノートと概要カードを出す | `[dig].daily_cap = 0` なら何もしない。関心プロファイルが必要 |
| `answer` | 深掘りカードへの質問に答え、Telegram で返す | 通常は poller が起動する。同時に 1 つだけ動く。関心プロファイルが必要 |
| `x-auth` | X OAuth 2.0(PKCE)の認可を行い token を保存する | `[x_oauth2].client_id` が必要(runbook §9) |
| `x-signals` | X の興味シグナル(ブックマーク・いいね・自分の RT)を取り込む | `--backfill` 初回の種まき(`--limit N` で kind ごとの件数。既定 100)/ `--kind bookmark\|like\|self_rt` |
| `x-insights` | X の興味シグナルの週次インサイトを表示する | 読み取り専用(migration も当てない) |
| `poll` | 受信デーモン(long polling で常駐、内蔵タイマー付き) | 通常は launchd が起動する |
| `run` | 朝バッチの全段を順に実行する | `--force` 当日のダイジェストを作り直し、Telegram にも送り直す |
| `doctor` | 14 項目を検査する。FAIL があれば exit 1 | `--purge` 期限切れの削除だけを行う / `--cost` 当月コストだけを表示する |

doctor の各項目は [設計 05 §2](docs/design/05-ops-runtime.md)。

## データと設定の置き場(既定値)

| もの | 既定の場所 | 変え方・補足 |
|---|---|---|
| 設定 | `~/.config/intel/`(`config.toml`・`.env`・`interest-profile.md`、X の OAuth 2.0 を使うなら `x-oauth2-token.json`) | `--config-dir`。プロファイルだけは `[analysis].profile_path` |
| DB | `~/.local/share/intel/intel.db` | `[storage].db_path` |
| ログ | `~/.local/state/intel/logs/`(`morning.log`・`poller.log`・`answer.log`。JSON Lines、日次で 14 世代) | launchd 側の出力は `morning-launchd.log`・`poller-launchd.log` |
| バックアップ | `~/Backups/intel/intel-YYYYMMDD.tar.gz`(chmod 600、日次で 14 世代。中身は DB・設定ディレクトリ・`prompts/`) | `[storage].backup_dir`。secrets を含むのでクラウド同期の外に置く |
| Obsidian の出力 | `<vault>/SignalForge/` の下の `Daily/`・`Items/`・`DeepDive/` | `[obsidian].vault_path` / `folder` |

## 運用

無人運用の手順は [ops/runbook.md](ops/runbook.md)。構成の理由は [設計 05](docs/design/05-ops-runtime.md) にある。

| 節 | 内容 |
|---|---|
| §1 | plist の生成(placeholder を置換し、lint してから `~/Library/LaunchAgents/` にコピー) |
| §2 | bootstrap(ロード)とアンロード |
| §3 | スリープ対策(pmset wake) |
| §4 | uv の実体バイナリへの Full Disk Access の付与 |
| §5 | セットアップ後の確認 |
| §6 | 配信時刻の変更 |
| §7 | バックアップと復旧 |
| §8 | secrets のローテーション |
| §9 | X OAuth 2.0 のセットアップ |
| §10 | X 興味シグナルの取り込みの有効化 |
| §11 | 深掘りカードへの質問への返事(既存のインストールの更新を含む) |

**コードを更新したとき**

```sh
before="$(git rev-parse HEAD)"
git pull
git diff --stat "$before" HEAD -- ops/launchd/ migrations/   # 雛形と migration の変更を確かめる
uv sync
```

- `ops/launchd/` に差分があれば、plist の雛形が変わっている。runbook §2 で 2 つのジョブを bootout し、§1 で plist を作り直してから、§2 で 2 つとも bootstrap し直す。コピーして配置しているので、雛形の変更は自動では反映されない。そのあと `uv run intel doctor` で確かめる。
- `ops/launchd/` に差分が無ければ、poller だけを入れ直す。

```sh
launchctl bootout "gui/$(id -u)/com.intel.poller"
launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.poller.plist"
uv run intel doctor
```

- `migrations/` に差分があれば、未適用の migration は入れ直した poller の起動時(または次の朝バッチ)に当たり、適用前に DB の `.bak`(DB と同じディレクトリの `intel.db.bak`)が自動で取られる。doctor の #3 が PASS であることを確かめる。
- doctor の #6 が `LLMUnavailableError` で FAIL したら、codex を更新する。新しいコードが無効化する codex の機能名を増やすことがあり、古い codex はその起動引数を受け付けない(原因の見分け方は「はじめかた」の手順 7)。
- 朝バッチは次の起動で新しいコードを使う。

## 開発

```sh
uv sync                # dev 依存(pytest・ruff)も入る
uv run pytest
uv run ruff check .
```

- CI は無い。PR ごとに手元で pytest と ruff を走らせる。
- 流れは GitHub Issue → 小さな PR → 相互レビュー → merge。コードを書いた AI とは別の AI がレビューし、must がゼロになったら merge する。migration を含む PR は「バックアップ → 適用 → doctor」をレビュー項目に入れる([設計 05 §9](docs/design/05-ops-runtime.md))。

repo 固有の決まり:

- **migration は forward-only**。適用済みのファイルは編集も改名もせず、新しい番号のファイルを足す([設計 01 §10](docs/design/01-data-model.md)、[app/db.py](app/db.py))。
- **prompt は既存のファイルを書き換えない**。新しい版のファイルを足し、それを指すモジュールの版の定数(`PROMPT_VERSION`。対訳だけは `app/deepdive.py` の `TRANSLATE_PROMPT_VERSION`)を更新する([設計 03 §5](docs/design/03-analysis-ranking.md))。
- **runbook の §番号を変えない**。コードのメッセージが §番号で参照している([app/cli.py](app/cli.py)、[app/insights.py](app/insights.py))。
- **LLM は隔離した `codex exec` で呼ぶ**。ツールや書き込み・送信の権限を LLM に与えず、vault への書き込みと Telegram への送信は Python 側が行う。外部の本文はプロンプトの中で実行ごとの nonce の囲みに入れ、データとして扱う([設計 00 §5](docs/design/00-architecture.md)、[設計 03 §3.2](docs/design/03-analysis-ranking.md))。

## ディレクトリ構成

| パス | 役割 |
|---|---|
| `app/` | 本体。朝バッチの段の実体(`collect.py`・`purge.py`・`backup.py`)と直列実行(`run.py`)、検査(`doctor.py`)、CLI(`cli.py`)、設定(`config.py`)、DB と migration のランナー(`db.py`)、共通部品(`http.py`・`jst.py`・`logs.py`・`models.py`・`normalize.py`・`prompting.py`)、X シグナルのインサイト(`insights.py`) |
| `app/connectors/` | ソースのコネクタ。RSS と X(ホーム TL・検索・OAuth 2.0・興味シグナル) |
| `app/extraction/` | 記事の本文抽出 |
| `app/analysis/` | LLM の抽象層、事前選別(Stage 1)、本分析(Stage 2)、トレンドまとめ、関心プロファイルの読み込み |
| `app/ranking/` | ランキング式 |
| `app/digest/` | ダイジェストの生成 |
| `app/delivery/` | Telegram の配信、受信デーモン、夜アンケートと watchdog と失敗通知、`intel answer` の起動 |
| `app/export/` | Obsidian への書き出し |
| `app/dig.py`・`app/deepdive.py`・`app/dig_answer.py`・`app/dig_take.py` | 深掘りのキュー(夜の入口)、朝の調査段、カードへの質問への返事、take の記録 |
| `migrations/` | 番号付きの SQL。`app/db.py` が順に当てる |
| `prompts/` | 版付きのプロンプト(analyze・trend・dig・dig-translate・dig-answer) |
| `ops/` | launchd の plist の雛形 2 本と runbook |
| `scripts/` | `kpi/` は KPI 集計の SQL(使い方は [docs/project/kpi-queries.md](docs/project/kpi-queries.md))。`day0_x_probe.py` は Day 0 の使い捨てスクリプト |
| `tests/` | pytest のテスト |
| `docs/` | 企画・設計・ADR・計画などの文書。一覧は [docs/README.md](docs/README.md) |
| `CONTEXT.md` | フェーズ 2 の用語集 |

## 文書

初めて読むときの順番:

1. この README
2. [docs/PROPOSAL.md](docs/PROPOSAL.md) — 企画(何を、なぜ作るか)。設計と食い違えば PROPOSAL が優先する
3. [docs/design/00-architecture.md](docs/design/00-architecture.md) — 全体の構成
4. 関心に応じて [docs/design/](docs/design/) の 01〜06 と [docs/adr/](docs/adr/)(覆しにくい設計判断)。フェーズ 2 の用語は [CONTEXT.md](CONTEXT.md)
5. 運用するなら [ops/runbook.md](ops/runbook.md)

全文書の一覧、正本の優先順位、ADR の一覧は [docs/README.md](docs/README.md) にまとめている。

## ライセンス

個人利用のため、ライセンスは付けていない。
