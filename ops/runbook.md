# 運用 runbook — セットアップ・復旧・secrets・X 連携(plan #22 から拡張)

設計正本: [../docs/design/05-ops-runtime.md](../docs/design/05-ops-runtime.md)(構成の理由は設計側)。
本書は手を動かす手順のみ。時刻の前提: システムのタイムゾーンが JST であること。

節の番号はコードのメッセージが参照しているので変えない。

- §1〜§5: launchd のセットアップ(plist の生成、bootstrap、スリープ対策、Full Disk Access、確認)
- §6: 配信時刻の変更
- §7: バックアップと復旧
- §8: secrets のローテーション
- §9〜§10: X OAuth 2.0 のセットアップと興味シグナルの取り込み
- §11: 深掘りカードへの質問への返事(既存のインストールの更新を含む)
- §12: ソースの見直し(週 1 回の健康診断を受けて止める・URL を直す)
- §13: 深掘りノートのレビュー(別系統の AI が見る。既定は off)
- §14: Codex が失敗したときの予備(Claude API でやり直す。既定は off)
- §15: 配信の記帳に失敗したとき(届いた 1 通を台帳に入れてから再走する)

## 前提

- `~/.config/intel/config.toml` と `.env`(chmod 600)が配置済み(`intel doctor` #1〜2 が PASS)
- 手動の `intel run` / `intel poll` が動くこと(先に動作確認してから無人化する)
- codex CLI が入り、`codex login` が済んでいること(`intel doctor` #6 が PASS)。配布の形・確かめた版・
  更新の手順は §1 の末尾

## 1. plist の生成(プレースホルダ置換)

`ops/launchd/*.plist` の `__PLACEHOLDER__` を実環境の値に置換し、一時ディレクトリで
lint を通してから `~/Library/LaunchAgents/` へ**コピー**する(symlink は macOS が
無視することがあるため不可。lint 前に設置すると壊れた plist が残るため tmp を経由)。
コピー先が無ければ作る。途中で失敗したらそこで止まるので、失敗したまま §2 に進まない。

リポジトリの root で:

```sh
UV_BIN="$(command -v uv)"
[ -n "$UV_BIN" ] || { echo "uv が見つからない(PATH を確認)"; exit 1; }
# 朝バッチの analyze は codex を subprocess 起動する。launchd の PATH に codex(と node)の
# dir が無いと全チャンク「codex not found」で degraded になる(#72)。setup 時に解決して plist の
# PATH に焼き込む。poller が起動する `intel answer`(深掘りカードへの質問への返事。GH#126)も
# codex を使うため、__CODEX_DIR__ は両方の plist で使う。
CODEX_BIN="$(command -v codex)"
[ -n "$CODEX_BIN" ] || { echo "codex が見つからない(PATH を確認 / codex login 済みか)"; exit 1; }
# npm 版の codex は先頭行が `#!/usr/bin/env node` の script で、起動に node が要る(配布の形は
# この節の末尾)。node が codex と別の dir にあると launchd の PATH では起動できないので、
# そのときは node の dir も __CODEX_DIR__ に `:` でつないで入れる(GH#221)
CODEX_PATH_DIRS="$(dirname "${CODEX_BIN}")"
NODE_BIN=""
if head -n 1 "${CODEX_BIN}" | grep -q '^#!.*node'; then
  NODE_BIN="$(command -v node)"
  [ -n "$NODE_BIN" ] || { echo "node が見つからない(npm 版の codex は Node.js が要る)"; exit 1; }
  [ "$(dirname "${NODE_BIN}")" = "${CODEX_PATH_DIRS}" ] \
    || CODEX_PATH_DIRS="${CODEX_PATH_DIRS}:$(dirname "${NODE_BIN}")"
fi
# sed 置換のため、パスに sed / XML のメタ文字を含む環境は対象外(先に検知して止める)
case "${UV_BIN}${CODEX_BIN}${NODE_BIN}$(pwd)${HOME}" in
  (*[\|\&\<\>]*) echo "パスに | & < > を含む環境は本手順のsed置換が壊れるため非対応"; exit 1;;
esac
mkdir -p "${HOME}/.local/state/intel/logs"   # launchd は exec 前に stdout/stderr を開く
# コピー先。新しい Mac・新しいユーザーでは、ほかのアプリが作るまで無い(GH#220)
mkdir -p "${HOME}/Library/LaunchAgents" || exit 1
TMP_DIR="$(mktemp -d)"
for name in com.intel.morning com.intel.poller; do
  sed -e "s|__UV_BIN__|${UV_BIN}|g" \
      -e "s|__UV_DIR__|$(dirname "${UV_BIN}")|g" \
      -e "s|__CODEX_DIR__|${CODEX_PATH_DIRS}|g" \
      -e "s|__REPO_DIR__|$(pwd)|g" \
      -e "s|__HOME__|${HOME}|g" \
      "ops/launchd/${name}.plist" > "${TMP_DIR}/${name}.plist"
  plutil -lint "${TMP_DIR}/${name}.plist" || exit 1
  # 生成した PATH だけで codex を起動できること(launchd は plist の PATH と HOME で起動する)。
  # node が見つからないなどを、無人の実行で失敗する前にここで見つける(GH#221)
  GEN_PATH="$(plutil -extract EnvironmentVariables.PATH raw -o - "${TMP_DIR}/${name}.plist")" || exit 1
  env -i PATH="${GEN_PATH}" HOME="${HOME}" codex --version >/dev/null \
    || { echo "${name}: 生成した PATH で codex を起動できない(codex と node の dir を確認)"; exit 1; }
  cp "${TMP_DIR}/${name}.plist" "${HOME}/Library/LaunchAgents/${name}.plist" || exit 1
done
rm -rf "${TMP_DIR}"
```

**Codex CLI の配布の形と版**(GH#221・GH#222):

- 対応する配布の形は 2 つ。npm の `@openai/codex`(`codex` は node で動く script なので Node.js が要る)と、
  Node.js の要らない単体の実行ファイル(Homebrew で入れたものなど)。上の手順は `codex` の先頭行で見分け、
  npm 版のときだけ node の dir も PATH に入れる。
- 動作と隔離(`intel doctor` #6)を確かめた版は `codex-cli 0.162.1`(`codex --version` の表示。
  2026-10-24 に、上の手順で生成した PATH の下で #6 が PASS)。npm 版で、`npm install -g @openai/codex@0.162.1`
  で入れた。
- 対応の範囲: 実装は `--strict-config`・permission profile のキー・`--disable` で外す feature 名に頼る
  (設計 00 §5)。これを受け付けない版(古い版や、feature 名を変えた新しい版)では codex が起動の時点で
  エラーで止まり、doctor #6 が FAIL する(起動引数が黙って無視されることはない)。例外の型は codex の stderr の
  書き出しで決まり、決まった書き出し(`Error: Unknown feature flag` など)なら `LLMUnavailableError`、
  それ以外は `LLMError` になる。例外とログには stderr の中身を載せず、当たった書き出しか終了コードだけが出る
  (`codex の起動設定エラー(Error: Unknown feature flag)`・`codex 非0終了(1)`。GH#202)。
  確かめたのは上の版だけなので、ほかの版は下の手順で doctor #6 を通してから使う。
- npm 版は node の版ごとの global に入る。node を別の版に替えると、PATH の上の `codex` が無くなるか
  別の版になる。新しい node の側にも上の版を入れ、この節の手順をやり直す(plist の PATH が変わったら
  morning と poller の両方を入れ直す。§11 の 2)。

**Codex CLI を更新するとき**(無人運用へ反映する前に doctor #6 を通す):

1. 新しい版を一時的な場所に入れ、PATH の先頭に置いて doctor を走らせる(npm 版の例。単体の実行ファイルも、
   一時的な dir に置いて同じように PATH の先頭に置く):
   ```sh
   CODEX_TRY="$(mktemp -d)"
   npm install -g --prefix "${CODEX_TRY}" @openai/codex@<新しい版> || exit 1
   "${CODEX_TRY}/bin/codex" --version
   PATH="${CODEX_TRY}/bin:${PATH}" uv run intel doctor   # #6 が PASS になること
   rm -rf "${CODEX_TRY}"
   ```
   #6 が FAIL なら更新しない(launchd のジョブは今の版のまま動き続ける)。
2. PASS したら、いつもの場所に入れる(`npm install -g @openai/codex@<新しい版>`)。launchd のジョブは
   PATH の上の `codex` をそのまま使うので、入れた時点から新しい版で動く。朝バッチ(06:30 から終わるまで)の
   間は避ける。
3. もう一度 `uv run intel doctor` を流し、#6 が PASS になることを確かめる。FAIL なら上の確かめた版に戻す
   (`npm install -g @openai/codex@0.162.1`)。codex か node の dir が変わったときは、この節の手順で plist を
   作り直し、morning と poller の両方を入れ直す(§11 の 2)。
4. 上の「確かめた版」の行を新しい版に書き換える。

## 2. bootstrap(ロード)

```sh
launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.morning.plist"
launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.poller.plist"
launchctl print "gui/$(id -u)/com.intel.morning" | head -5   # state を確認
launchctl print "gui/$(id -u)/com.intel.poller" | head -5
```

アンロード(停止・設定変更時):

```sh
launchctl bootout "gui/$(id -u)/com.intel.morning"
launchctl bootout "gui/$(id -u)/com.intel.poller"
```

## 3. スリープ対策(pmset wake)

`StartCalendarInterval` はスリープ中スキップされるため、朝バッチ(06:30)の5分前に wake を予約する:

```sh
sudo pmset repeat wakeorpoweron MTWRFSU 06:25:00
pmset -g sched   # 予約の確認
```

## 4. Full Disk Access(FDA)の付与

launchd 背景ジョブからの Obsidian Vault 書き込みが TCC で止まるため、`uv` の**実体バイナリ**に
FDA を付与する:

1. `readlink -f "$(command -v uv)"` で実体パスを確認する(symlink 先が実体)。
2. システム設定 → プライバシーとセキュリティ → フルディスクアクセス → `+` で
   上記バイナリを追加して有効化する(Finder で `Cmd+Shift+G` → パス貼り付け)。
3. 付与確認は `intel doctor`(#7 Vault 書込テスト)で行う。

## 5. セットアップ後の確認

1. `uv run intel doctor` — #8(launchd 2ジョブ)が PASS になる。FAIL/WARN が残る項目を解消する。
2. 受信デーモン: Telegram でボタン付きメッセージに反応し、`✓` が付くこと。
3. **翌朝の無人配信**: 朝バッチ後にダイジェストが Telegram に届き、
   `~/.local/state/intel/logs/morning.log` に当日 run の JSON 行があること
   (これが plan #22 の完了判定。届かない場合は watchdog 通知 → 手動 `intel run`)。

## 6. 配信時刻の変更(宿題 H5 の確定時)

1. 雛形 `ops/launchd/com.intel.morning.plist` の `StartCalendarInterval`(Hour/Minute)を配信時刻の30分前に変更。
   `~/Library/LaunchAgents/` に配置済みの plist は直さない(手順 4 の §1 の再生成で雛形から上書きされ、元の時刻に戻る)。
   雛形の時刻は `tests/test_ops.py`(`test_morning_plist_contract`)が固定し、設計 05 §1・本書 §3・README にも書いてあるので、同じ変更で合わせる。
2. `sudo pmset repeat wakeorpoweron MTWRFSU <バッチ5分前>` を再設定。
3. `config.toml` の `[telegram] watchdog_time` を配信時刻+15分に変更。
4. §2 の bootout → §1 の再生成 → bootstrap で反映(poller も入れ直す。`watchdog_time` は poller が起動時に読むため)。

## 7. バックアップと復旧(設計 05 §5 / plan #23)

バックアップは朝バッチ末尾(backup 段)で自動作成される:
`~/Backups/intel/intel-YYYYMMDD.tar.gz`(chmod 600・日次14世代。置き場は
`config.toml` の `[storage] backup_dir` で変更可)。

**セットアップ時の確認(1回)**:

- `~/Backups/` が iCloud 等のクラウド同期対象外であることを確認する
  (secrets 入り tar.gz を同期経路に流さない。設計 05 §5)。
- 手動で `uv run intel run` を1回流し、`intel doctor` #10 が PASS になること。

**復旧手順**(リハーサルを1回実施すること):

```sh
# 0. 書き込みプロセスを止める(復旧中の DB 書込を防ぐ)。bootout の成否だけで止まったと扱わず、
#    下の (a)(b) で何も出なくなるまで先へ進まない
launchctl bootout "gui/$(id -u)/com.intel.morning" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.intel.poller" 2>/dev/null || true
#    (a) 2 ジョブが外れたこと。launchctl print が「見つからない」(exit 113)で終わるときだけ外れたと
#    みなす。出たら(exit 0 = まだロードされている、ほかの exit = 確かめられない)bootout をやり直すか
#    原因を調べる(poller が残ると KeepAlive で起動し直す)
for job in com.intel.morning com.intel.poller; do
  err="$(launchctl print "gui/$(id -u)/${job}" 2>&1 >/dev/null)"; rc=$?
  [ "${rc}" -eq 113 ] || echo "${job}: 外れたと確かめられない(exit ${rc}) ${err}"
done
#    (b) intel のプロセスが残っていないこと。poller が起動した `intel answer`(python -m app.cli answer)は
#    別セッションで動くので、poller を止めても終わらずに残りうる(設計 05 §1.2)。手で起動した intel
#    コマンド(`uv run intel …` の uv が環境を整えている間も含む)も bootout では止まらない。一覧が
#    出たら終わるのを待ち(返事を作っている answer は LLM の待ちで数分以上かかることがある)、もう一度
#    実行する
pgrep -fl 'app\.cli|(^|[ /])intel( |$)'

RESTORE_DIR="$(mktemp -d)"
tar -xzf "${HOME}/Backups/intel/intel-<日付>.tar.gz" -C "${RESTORE_DIR}"

# 1. 設定類を先に配置(必要な場合のみ。.env は chmod 600 を維持)
cp -a "${RESTORE_DIR}/config/." "${HOME}/.config/intel/"
chmod 600 "${HOME}/.config/intel/.env"

# 1b. アーカイブに profile/ があれば(関心プロファイルを設定ディレクトリの外に置いていた場合。
#     GH#199)、そのファイルを、展開した config.toml の [analysis].profile_path が指す場所
#     (未設定なら設定ディレクトリの interest-profile.md)へ置く。その場所が symlink なら、
#     リンクの先へ置くか、リンクをこのファイルで置き換える。置かないと分析・深掘り・返事が
#     止まる(設計 05 §5)。ls で何も出なければこの手順は飛ばす。profile_path が相対パスなら、
#     intel を動かすディレクトリ(launchd の WorkingDirectory = repo)からのパスなので、
#     絶対パスに直して PROFILE_PATH に書く
ls "${RESTORE_DIR}/profile/" 2>/dev/null
PROFILE_PATH="${HOME}/.config/intel/interest-profile.md"   # ← [analysis].profile_path 設定時は書き換える
mkdir -p "$(dirname -- "${PROFILE_PATH}")"
cp -p "${RESTORE_DIR}/profile/<ファイル名>" "${PROFILE_PATH}"

# 2. 復旧先 DB パスを確認する。config.toml の [storage].db_path を設定していれば
#    その値、未設定なら既定の ~/.local/share/intel/intel.db
DB_PATH="${HOME}/.local/share/intel/intel.db"   # ← [storage].db_path 設定時は書き換える

# 3. 補助の確認: DB を開いているプロセスが無いこと(手順 0 の (b) に出ない sqlite3 なども含む)。
#    一覧が出たら閉じるか終わるのを待ってから、もう一度実行する。何も出なくなってから次へ進む
[ -f "${DB_PATH}" ] && lsof -- "${DB_PATH}"

# 4. 既存 DB 一式を退避してから配置する。WAL/SHM を残すと、復元した本体に
#    古い WAL が適用されて不整合になるため、必ず 3 ファイルまとめて扱う
mkdir -p "${DB_PATH%/*}"
for suffix in "" "-wal" "-shm"; do
  [ -f "${DB_PATH}${suffix}" ] && mv "${DB_PATH}${suffix}" "${DB_PATH}${suffix}.pre-restore"
done
mv "${RESTORE_DIR}/intel.db" "${DB_PATH}"

# 5. 健全性確認 → 問題なければ launchd を再 bootstrap(§2)
uv run intel doctor
```

Obsidian Vault はバックアップ対象外。Daily / Items ノートの生成部分は DB から書き出せる(ただし通常の `intel run` の export が書くのは当日分と、まだ反映していない感想のある分だけで、過去分を一括では書き直さない)が、DeepDive ノートと、ノートに足した手書きのメモ・take は vault にしか無いので、vault は別に保全する(設計 05 §5)。

## 8. secrets ローテーション(設計 05 §6)

- X: Developer Portal で再発行 → `.env` 差し替え → `intel doctor`
  (朝バッチは毎回 `.env` を読むので入れ直しは要らない)
- Telegram: BotFather で revoke → 再発行 → `.env` 差し替え → `intel doctor` → poller を入れ直す
  (allowlist は chat_id 不変なので変更不要)。poller は起動時に token を読んで持ち続けるので、
  入れ直すまでは失効した token で再接続を繰り返し、受信・夜アンケート・watchdog が止まったままになる
  (doctor は `.env` の新しい token で PASS するので、これでは気づけない)。朝バッチと `intel answer` は
  毎回 `.env` を読むので入れ直しは要らない。
  ```sh
  launchctl bootout "gui/$(id -u)/com.intel.poller"
  launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.poller.plist"
  ```
  確認: §5 の 2 と同じく、Telegram でボタン付きメッセージに反応し、`✓` が付くこと。

## 9. X OAuth 2.0 セットアップ(S1 = GH#58・設計 02 §2)

ブックマーク/いいね取込(S1)に必要。ホームTL・検索(OAuth 1.0a の4キー)とは別の認証。

1. **X Developer Portal**: 対象アプリの User authentication settings で OAuth 2.0 を有効化
   - Type of App: **Native App(public client)を推奨**(client secret 不要)。
     Web App(confidential)にした場合のみ client secret を `.env` の
     `X_OAUTH2_CLIENT_SECRET` に置く(chmod 600)
   - App permissions: Read
   - Callback URI: `http://127.0.0.1:8791/callback`
     (config `[x_oauth2].callback_port` を変えた場合は合わせる。**完全一致必須**)
2. **config.toml** に Client ID を記入:
   ```toml
   [x_oauth2]
   client_id = "<OAuth 2.0 Client ID>"
   ```
3. **認可フローを実行**(ブラウザが開く → X で承認):
   ```sh
   uv run intel x-auth
   ```
   完了すると `~/.config/intel/x-oauth2-token.json`(chmod 600)が生成され、
   `@ユーザー名` が表示される。token は refresh で自動更新される(手編集しない)
4. **確認**: `uv run intel doctor` の #13(X OAuth2 token)が PASS
5. **無効化 / 再認可**: X の設定 → アプリ連携から revoke + token ファイル削除。
   再認可は `intel x-auth` を再実行(refresh 失敗時も同様)

## 10. X 興味シグナル取込の有効化(S1b = GH#58・設計 02 §2)

前提: §9(OAuth 2.0 セットアップ)が完了していること(`intel doctor` #13 = PASS)。

1. **予算の再配分**: 合算上限(165/日)の内側で取込枠を作る。例:
   ```toml
   [x]
   daily_read_cap = 115   # 130 から 15 を取込へ再配分
   [x_signals]
   daily_read_cap = 15
   ```
   (内訳合計 > combined_daily_read_cap は起動時 ConfigError)
2. **cold start(任意・初回のみ)**: kind ごと直近 N 件(既定 100)を種にする:
   ```sh
   uv run intel x-signals --backfill --limit 100
   ```
   最悪コスト = 3 kind × N × $0.004(既定 100 なら ≤ $1.2。GH#84)
3. 以降は**朝バッチが毎日差分を自動取込**(x_signals 段)。確認は
   `sqlite3 ~/.local/share/intel/intel.db "SELECT kind, count(*) FROM x_signals GROUP BY kind"`

## 11. 深掘りカードへの質問への返事(GH#126・設計 05 §1.2)

poller が起動する `intel answer` は codex を使う。poller.plist の PATH に codex の dir が
無いと、返事は全件「答えを作れませんでした」になる(#72 と同じ理由)。plist は**コピー配置**
なので、コードを更新して poller を再起動しただけでは古い PATH のまま残る。

**既存のインストールを更新するとき**(PATH に codex の dir が入る前の poller.plist を使っている場合):

1. §1 を実行して plist を再生成・コピーする(morning.plist も上書きされる)。
2. poller と morning を入れ直す。launchd はロードしたときの plist の内容を使い続けるので、コピーし直した
   だけでは PATH は変わらない(npm 版の codex で node の dir が足されたときは morning の PATH も変わる)。
   朝バッチ(06:30 から終わるまで)の間は避ける(bootout は動いているジョブを止める):
   ```sh
   launchctl bootout "gui/$(id -u)/com.intel.poller"
   launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.poller.plist"
   launchctl bootout "gui/$(id -u)/com.intel.morning"
   launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.morning.plist"
   # PATH に codex の dir(npm 版で node が別の dir なら node の dir も)
   launchctl print "gui/$(id -u)/com.intel.poller" | grep -A3 "environment"
   launchctl print "gui/$(id -u)/com.intel.morning" | grep -A3 "environment"
   ```
3. 確認: 深掘りカードにリプライし、「📝 記録しました。返事を作っています」の後に返事が届くこと。
   ログは `~/.local/state/intel/logs/answer.log`(子の起動失敗・異常終了は `poller.log`)。

- 古い PATH のまま受け付けた質問は「答えを作れませんでした」で確定し、自動では作り直さない。
  更新後にもう一度リプライする。
- 1 日の上限は `[dig].answer_daily_cap`(既定 20)。`0` にすると返事をせず感想の記録だけになる。
- カードのボタン(GH#141): 📝 要約・📖 詳しく は質問と同じ流れ(同じ 1 日の上限・同じ `answer.log`)。新しいカードから付く(既に送ったカードには付かない)。
- 調べ直しの依頼(GH#137): 「この観点でもう一度調べて」のようなリプライは、返事の末尾に「次の朝の深掘りで調べ直します」が付き、`dig_queue` に積まれる(観点 = リプライの原文)。次の朝の深掘り(`[dig].daily_cap` を共有)で、新しい解説ノートとカードが届く(前回のノートには frontmatter の `previous` でつながる)。積まれなかった依頼は `answer.log` に ERROR が出る。取り消すには、未消化一覧(どの日のものでもよい)へのリプライで「<番号> パス」と送る(番号はカードの「🔎 深掘り #番号」)。
- take の記録(GH#106): LLM が結論・感想(take)と分けたリプライは、`intel answer` が返事を送った後に
  解説ノートの末尾へ原文を足す(ノートは書き直さず追記だけ。frontmatter の status は変えない)。vault に書けないときは
  `answer.log` に ERROR が出て、poller が 10 分おきに `intel answer` を起動して再試行する
  (poller を入れ直すまでは、次にリプライしたときにだけ再試行する)。
- vault が応答しないとき(GH#145): `intel answer` は vault への 1 回のアクセスを 10 秒で打ち切り、
  その実行では vault に触れずに返事を作る(材料にノートが入らない)。take は記録せず次の実行に回す。
  `answer.log` に「vault が応答しなかった」の ERROR が出たら、macOS のプライバシーの確認
  (書類フォルダへのアクセス。対象は uv)が出ていないかを Mac の画面で見る。出ていれば許可する。
  見分け方:
  ```sh
  log show --last 30m --predicate 'process == "tccd" AND eventMessage CONTAINS "AUTHREQ_PROMPTING"'
  ```
  確認を出さないようにするには、§4 の FDA を uv の実体に付け直す(uv を更新したときは付け直しが要る)。
  `intel doctor` の #7(書込テスト)も同じ 10 秒で打ち切り、「書込テストが応答しない」の FAIL にして
  残りの検査へ進む(GH#248)。
  vault のファイルを自分のシェルで開いて確かめるときも、時間の上限を付ける
  (`perl -e 'alarm 5; exec @ARGV' head -c 1 <file>`。上限なしだとシェルも止まる)。
- 朝の深掘りのノートの公開(GH#234): `intel run` の dig 段と `intel dig` も、vault への書き出しを
  1 件ごとに 10 秒で打ち切り、カードの送信と後続の段へ進む。`morning.log` に「vault が応答しないので、
  ノートの公開をこの実行では止める」の ERROR が出て、朝バッチなら dig 段の失敗通知が届く。確かめ方と
  対処は上と同じ。公開できなかった件の本文は台帳に残り、次の `intel run` か `intel dig` が公開してから
  カードを送る(`intel dig` はその日の上限に達していても、未公開のノートと未送信のカードだけを進める)。
  「別の実行のノートの公開が終わっていない」の ERROR は、vault が止まった実行(朝バッチなど)が
  まだ残っているときに出る。その実行が終わってから(`pgrep -fl 'intel (run|dig)'` で確かめる)
  `intel dig` をやり直す。

## 12. ソースの見直し(GH#150・設計 02 §3)

毎週日曜の夜(夜アンケートの時刻)に poller が「🩺 ソースの健康診断」を送る。止める・URL を直すかは
人が決める(自動では変えない)。ソースの定義の正本は DB の `sources`(設計 01 §3)。

```sh
db=~/.local/share/intel/intel.db   # [storage].db_path を設定していればその値
sqlite3 -readonly -header -column "$db" \
  "SELECT id, type, name, enabled, consecutive_failures, moved_url, config FROM sources ORDER BY id;"
```

- **移転(↪️)**: 移転先がフィードを返すことを確かめてから、config の URL を記録済みの移転先に
  書き換える(URL を手で貼らない。`'` や `$` を含む URL で SQL やシェルが壊れないように、DB の
  `moved_url` をそのまま使い、SQL は引用符付きのヒアドキュメントで渡す)。
  記事は記事の URL で重複を見分けるので、フィードの URL を変えても同じ記事は二重に入らない。
  次に移転なしで取れた朝に、健康診断から消える(`moved_url` が空に戻る)。
  ```sh
  sqlite3 "$db" <<'SQL'
  UPDATE sources SET config = json_set(config, '$.url', moved_url)
  WHERE id = <id> AND moved_url IS NOT NULL;
  SQL
  ```
- **失敗が続く(⚠️)・新しい記事が入らない(💤)・👍・🔖 の率が低い(📊)**: 原因を見て、直せないなら止める。
  止めても行と記事は残る(戻すときは `enabled = 1`)。X のソースは止める前に設計 02 §2 の予算と検索スロットも見る。
  ```sh
  sqlite3 "$db" "UPDATE sources SET enabled = 0 WHERE id = <id>;"
  ```
- 新しい RSS を足すときも同じ表に行を入れる。SQL は引用符付きのヒアドキュメントで渡す(シェルに
  展開させない)。名前や URL に `'` が含まれるときは `''` と重ねる。
  ```sh
  sqlite3 "$db" <<'SQL'
  INSERT INTO sources (type, name, config) VALUES ('rss', '<名前>', json_object('url', '<URL>'));
  SQL
  ```

## 13. 深掘りノートのレビュー(GH#153・設計 06 §4)

朝の深掘りの解説ノートを、書き手(Codex)とは別系統の AI(Claude API)が元記事と照らして
レビューし、指摘があれば書き手が 1 回だけ直す。既定は off。

**有効にする**:

1. Anthropic のコンソールで API キーを発行し、月の利用上限(spend limit)も設定しておく。
2. `~/.config/intel/.env` に `INTEL_ANTHROPIC_API_KEY=<キー>` を足す。名前を `ANTHROPIC_API_KEY`
   にしない(環境に載ると claude -p が無自覚に API 課金へ回る。doctor #12 が WARN にする)。
3. `config.toml` に足す:
   ```toml
   [dig]
   review = true
   # review_monthly_cap_usd = 10.0   # 月(UTC の暦月)の上限。達したらその月はレビューを省く

   # [anthropic_api]                  # 既定は Opus 5.5・effort medium(モデルを替えたら単価も)
   # model = "claude-opus-5-5"
   ```
4. `uv run intel doctor` で #2 が PASS になること(キーが無いと FAIL)。
5. 次の朝のカードに「🧪 別 AI のレビュー: …」の行が付く。ノートの末尾(take 領域の上)に指摘が残る。

朝バッチは毎回 `.env` と config を読むので、入れ直しは要らない(poller も入れ直し不要)。

**結果を見る**(1〜2 週間回して、続けるかを決める材料):

```sh
db=~/.local/share/intel/intel.db   # [storage].db_path を設定していればその値
sqlite3 -readonly -header -column "$db" <<'SQL'
SELECT date(created_at, '+9 hours') AS day, item_id, review_status,
       json_array_length(review_findings) AS findings, round(review_cost_usd, 3) AS usd
FROM dig_results WHERE review_status IS NOT NULL ORDER BY id DESC LIMIT 20;
SQL
```

- 指摘の中身は `review_findings`(JSON)か、ノート末尾の記録で読む。
- `failed` が続くときは `morning.log` の「レビュー失敗」の WARNING を見る。401/403 はキー、404 はモデル名、
  400 はクレジット残高の不足や利用上限のことがある。
- 当月の費用は `uv run intel doctor --cost` の LLM に入る。

**止める**: `[dig].review = false`(キーは残してよい)。

## 14. Codex が失敗したときの予備(GH#156・設計 00 §5)

Codex(主のエンジン)が上限・障害・ログイン切れで失敗した LLM の呼び出しだけを、Claude API
(既定 Sonnet 5.5)でやり直す。対象は記事の判定・トレンド・深掘り(解説・対訳・指摘を受けた直し)・
カードへの返事。残量は見ず、失敗そのものを合図にする。既定は off。

**有効にする**: §13 と同じキー(`.env` の `INTEL_ANTHROPIC_API_KEY`)を使う。`config.toml` に足す:

```toml
[llm_fallback]
engine = "anthropic-api"
# monthly_cap_usd = 10.0   # 予備の月(UTC の暦月)の上限。達したらその月は予備を使わない
# model = "claude-sonnet-5-5"   # 替えたら input_usd_per_mtok / output_usd_per_mtok も
```

`uv run intel doctor` の #2 が PASS になること(キーが無いと FAIL)。朝バッチ・`intel answer` は毎回
config を読むので、poller の入れ直しは要らない。

**「⚠️ Codex(主のエンジン)が失敗したため、Claude API で代行しました」が届いたら**:

1. 失敗の種類を見る。`morning.log` の「主のエンジンが失敗したため予備(…)で代行(主: …)」の WARNING に、
   Codex の失敗の種類(`codex 非0終了(1)`・`タイムアウト`・`codex 起動失敗` など)が出る(本文は書かない)。
2. Codex の状態を確かめる。`codex login status`(ログイン切れなら `codex login`)。`uv run intel doctor` の
   #6 で、Codex が答えられるかを確かめる。上限なら `codex` を対話で起動して残りを見る。回復を待つ間も予備が動く。
   - 「うち予備も失敗した呼び出し」は呼び出しの回数で、段の中のやり直し(1 回)で答えられたものも含む。最終的に
     劣化・失敗したかは、朝バッチの失敗通知・ダイジェストの劣化の表示・深掘りのカードの有無と、`morning.log` の
     「主のエンジンも予備も失敗」の WARNING で見る。
3. 費用は知らせの「今月の予備」と `uv run intel doctor --cost` の LLM で見る。上限は呼び出しの前に
   判定し、費用は呼び出しの後に記録するので、始めた呼び出しの分だけ上限を超えることがある(1 つだけ
   走っていれば 1 回分。朝バッチとカードへの返事は同時に走りうるので、そのときは同時に始めた分)。上限に達した月は予備を使わず、
   Codex が失敗した呼び出しは劣化・失敗に戻る(ログに「予備は今月の上限 … に達しているので使わない」)。

予備の呼び出しの記録:

```sh
db=~/.local/share/intel/intel.db   # [storage].db_path を設定していればその値
sqlite3 -readonly -header -column "$db" <<'SQL'
SELECT created_at, run_id, stage, primary_error, ok, round(cost_usd, 3) AS usd
FROM llm_fallback_calls ORDER BY id DESC LIMIT 20;
SQL
```

- `primary_error` が `skipped` の行は、同じ実行で Codex が続けて失敗した後に、Codex を呼ばずに予備へ
  行った呼び出し(Codex に戻るのは次の実行から)。
- カードへの返事(朝バッチの外)で予備を使ったときは知らせない。`answer.log` の WARNING と、上の表の
  `run_id` が空の行で見る。

**止める**: `[llm_fallback].engine = ""`(または行ごと消す)。

## 15. 配信の記帳に失敗したとき(GH#225・設計 04 §2)

digest の配信で、Telegram には送れたのに台帳(`digest_messages`)に記帳できなかったときは、送り直さず
記帳だけを 3 回までやり直す。それでも駄目なら配信を止め、ERROR「送信済みの message を台帳に記帳できない」を
`morning.log` に残す(朝バッチも手動の `intel deliver` も同じファイル。日をまたいだら `morning.log.<日付>`)。
朝バッチなら失敗通知の段は `deliver` になる。

この 1 通は届いているが台帳に無い。このまま再走すると同じ記事をもう一度送り、先に届いた方への
リプライは感想として記録されない。再走の前に、次の手順で台帳に入れる。

1. ログから値を取る(`item_id` が null の行はヘッダ。朝バッチの段の失敗の行にも同じ文が入るので、
   `message_id` を持つ行だけに絞る):
   ```sh
   grep '台帳に記帳できない' ~/.local/state/intel/logs/morning.log \
     | jq -c 'select(.extra.message_id? != null)
              | {item_id, digest_id: .extra.digest_id, message_id: .extra.message_id, error: .extra.error}'
   ```
2. `error` の原因(`database or disk is full` ならディスクの空きなど)を直す。
3. 台帳に入れる(値は 1 の出力。ヘッダは `item_id` を `NULL` にする):
   ```sh
   db=~/.local/share/intel/intel.db   # [storage].db_path を設定していればその値
   sqlite3 "$db" "INSERT INTO digest_messages (digest_id, item_id, message_id) VALUES (<digest_id>, <item_id>, <message_id>);"
   ```
4. `uv run intel deliver` で残りを送る(台帳にある分は送らない)。`intel deliver` は当日の digest だけを
   送るので、日をまたいだら残りは送らない(届かなかった記事は Obsidian の Daily ノートで見る)。

- 配信は同じ DB に対して 1 つずつ走る(DB の隣の `intel.db.deliver.lock`。GH#192)。朝バッチの配信中に手で
  `intel deliver` を打つと、先の配信が終わるまで待ってから、未送信の分だけを送る。
