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

## 前提

- `~/.config/intel/config.toml` と `.env`(chmod 600)が配置済み(`intel doctor` #1〜2 が PASS)
- 手動の `intel run` / `intel poll` が動くこと(先に動作確認してから無人化する)

## 1. plist の生成(プレースホルダ置換)

`ops/launchd/*.plist` の `__PLACEHOLDER__` を実環境の値に置換し、一時ディレクトリで
lint を通してから `~/Library/LaunchAgents/` へ**コピー**する(symlink は macOS が
無視することがあるため不可。lint 前に設置すると壊れた plist が残るため tmp を経由)。

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
# sed 置換のため、パスに sed / XML のメタ文字を含む環境は対象外(先に検知して止める)
case "${UV_BIN}${CODEX_BIN}$(pwd)${HOME}" in
  (*[\|\&\<\>]*) echo "パスに | & < > を含む環境は本手順のsed置換が壊れるため非対応"; exit 1;;
esac
mkdir -p "${HOME}/.local/state/intel/logs"   # launchd は exec 前に stdout/stderr を開く
TMP_DIR="$(mktemp -d)"
for name in com.intel.morning com.intel.poller; do
  sed -e "s|__UV_BIN__|${UV_BIN}|g" \
      -e "s|__UV_DIR__|$(dirname "${UV_BIN}")|g" \
      -e "s|__CODEX_DIR__|$(dirname "${CODEX_BIN}")|g" \
      -e "s|__REPO_DIR__|$(pwd)|g" \
      -e "s|__HOME__|${HOME}|g" \
      "ops/launchd/${name}.plist" > "${TMP_DIR}/${name}.plist"
  plutil -lint "${TMP_DIR}/${name}.plist" || exit 1
  cp "${TMP_DIR}/${name}.plist" "${HOME}/Library/LaunchAgents/${name}.plist"
done
rm -rf "${TMP_DIR}"
```

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

1. plist の `StartCalendarInterval`(Hour/Minute)を配信時刻の30分前に変更。
2. `sudo pmset repeat wakeorpoweron MTWRFSU <バッチ5分前>` を再設定。
3. `config.toml` の `[telegram] watchdog_time` を配信時刻+15分に変更。
4. §2 の bootout → §1 の再生成 → bootstrap で反映。

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
# 0. 書き込みプロセスを止める(復旧中の DB 書込を防ぐ)
launchctl bootout "gui/$(id -u)/com.intel.morning" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.intel.poller" 2>/dev/null || true

RESTORE_DIR="$(mktemp -d)"
tar -xzf "${HOME}/Backups/intel/intel-<日付>.tar.gz" -C "${RESTORE_DIR}"

# 1. 設定類を先に配置(必要な場合のみ。.env は chmod 600 を維持)
cp -a "${RESTORE_DIR}/config/." "${HOME}/.config/intel/"
chmod 600 "${HOME}/.config/intel/.env"

# 2. 復旧先 DB パスを確認する。config.toml の [storage].db_path を設定していれば
#    その値、未設定なら既定の ~/.local/share/intel/intel.db
DB_PATH="${HOME}/.local/share/intel/intel.db"   # ← [storage].db_path 設定時は書き換える

# 3. 既存 DB 一式を退避してから配置する。WAL/SHM を残すと、復元した本体に
#    古い WAL が適用されて不整合になるため、必ず 3 ファイルまとめて扱う
mkdir -p "${DB_PATH%/*}"
for suffix in "" "-wal" "-shm"; do
  [ -f "${DB_PATH}${suffix}" ] && mv "${DB_PATH}${suffix}" "${DB_PATH}${suffix}.pre-restore"
done
mv "${RESTORE_DIR}/intel.db" "${DB_PATH}"

# 4. 健全性確認 → 問題なければ launchd を再 bootstrap(§2)
uv run intel doctor
```

Obsidian Vault はバックアップ対象外(SQLite が正本。`intel run` の export 段で再生成される)。

## 8. secrets ローテーション(設計 05 §6)

- X: Developer Portal で再発行 → `.env` 差し替え → `intel doctor`
- Telegram: BotFather で revoke → 再発行 → `.env` 差し替え → `intel doctor`
  (allowlist は chat_id 不変なので変更不要)

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

1. §1 を実行して plist を再生成・コピーする(morning.plist も同じ内容で上書きされる)。
2. poller を入れ直す(morning は次回起動で新しい plist を読むので入れ直し不要):
   ```sh
   launchctl bootout "gui/$(id -u)/com.intel.poller"
   launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/com.intel.poller.plist"
   launchctl print "gui/$(id -u)/com.intel.poller" | grep -A3 "environment"   # PATH に codex の dir
   ```
3. 確認: 深掘りカードにリプライし、「📝 記録しました。返事を作っています」の後に返事が届くこと。
   ログは `~/.local/state/intel/logs/answer.log`(子の起動失敗・異常終了は `poller.log`)。

- 古い PATH のまま受け付けた質問は「答えを作れませんでした」で確定し、自動では作り直さない。
  更新後にもう一度リプライする。
- 1 日の上限は `[dig].answer_daily_cap`(既定 20)。`0` にすると返事をせず感想の記録だけになる。
