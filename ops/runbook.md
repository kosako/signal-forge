# 運用 runbook — launchd セットアップ(plan #22)

設計正本: [../docs/design/05-ops-runtime.md](../docs/design/05-ops-runtime.md) §1(構成の理由は設計側)。
本書は手を動かす手順のみ。時刻の前提: システムのタイムゾーンが JST であること。

## 前提

- `~/.config/intel/config.toml` と `.env`(chmod 600)が配置済み(`intel doctor` #1〜2 が PASS)
- 手動の `intel run` / `intel poll` が動くこと(先に動作確認してから無人化する)

## 1. plist の生成(プレースホルダ置換)

`ops/launchd/*.plist` の `__PLACEHOLDER__` を実環境の値に置換して
`~/Library/LaunchAgents/` へ**コピー**する(symlink は macOS が無視することがあるため不可)。

リポジトリの root で:

```sh
UV_BIN="$(command -v uv)"
for name in com.intel.morning com.intel.poller; do
  sed -e "s|__UV_BIN__|${UV_BIN}|g" \
      -e "s|__UV_DIR__|$(dirname "${UV_BIN}")|g" \
      -e "s|__REPO_DIR__|$(pwd)|g" \
      -e "s|__HOME__|${HOME}|g" \
      "ops/launchd/${name}.plist" > "${HOME}/Library/LaunchAgents/${name}.plist"
done
plutil -lint "${HOME}/Library/LaunchAgents/com.intel."*.plist
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

## 7. secrets ローテーション(設計 05 §6)

- X: Developer Portal で再発行 → `.env` 差し替え → `intel doctor`
- Telegram: BotFather で revoke → 再発行 → `.env` 差し替え → `intel doctor`
  (allowlist は chat_id 不変なので変更不要)
