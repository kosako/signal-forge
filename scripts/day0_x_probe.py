#!/usr/bin/env python3
"""Day 0: X API 実測スクリプト(Issue #1・使い捨て・vibe 品質)

逆時系列ホームTLを max_results=100 × 最大5ページ取得し、以下を出力する:
- created_at の範囲・単調性(新→旧)
- author 別件数 CSV(day0_out/authors.csv)+ 全件 CSV(day0_out/items.csv)
- 各レスポンスの x-rate-limit-* ヘッダ
- 総読取件数と想定単価別のコスト早見(実効単価は課金メーターと突合して確定)

任意で --list-id を渡すと GET /2/lists/{id}/tweets を1ページ小確認する(リスト方式の再検討材料)。

認証: OAuth 1.0a user context(ホームTLは app-only Bearer 不可)。
repo root の .env(chmod 600・git 管理外)に以下を置く:
    X_API_KEY=...            # Developer Portal の API Key (Consumer Key)
    X_API_SECRET=...
    X_ACCESS_TOKEN=...       # 同 Portal で自分のアカウント用に発行した Access Token
    X_ACCESS_TOKEN_SECRET=...

実行:
    uv run --with requests --with requests-oauthlib scripts/day0_x_probe.py
    uv run --with requests --with requests-oauthlib scripts/day0_x_probe.py --pages 5 --list-id 123456
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "day0_out"
API = "https://api.twitter.com/2"

TWEET_FIELDS = "created_at,author_id,entities,referenced_tweets,in_reply_to_user_id"
EXPANSIONS = "author_id"

# 表計算ソフトが数式として解釈しうるセルの先頭の文字(CSV インジェクション。GH#246)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(value: str) -> str:
    """外部由来の文字列を、表計算ソフトで開いても文字列のセルになる形にする(GH#246)。

    先頭が数式の記号(= + - @)・タブ・CR なら先頭に ' を付け、それ以外はそのまま返す。
    区切りと引用符の処理は csv.writer に任せる。
    """
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists():
        sys.exit(f"ERROR: {path} がない。ヘッダのコメントどおり .env を作成して(chmod 600)")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def make_auth(env: dict[str, str]) -> OAuth1:
    keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in keys if not env.get(k)]
    if missing:
        sys.exit(f"ERROR: .env に {', '.join(missing)} がない")
    return OAuth1(env["X_API_KEY"], env["X_API_SECRET"],
                  env["X_ACCESS_TOKEN"], env["X_ACCESS_TOKEN_SECRET"])


def show_rate_limit(label: str, r: requests.Response) -> None:
    lim = r.headers.get("x-rate-limit-limit", "?")
    rem = r.headers.get("x-rate-limit-remaining", "?")
    reset = r.headers.get("x-rate-limit-reset", "?")
    reset_str = reset
    if reset not in ("?", ""):
        try:
            reset_str = datetime.fromtimestamp(int(reset), tz=timezone.utc).astimezone().strftime("%H:%M:%S")
        except ValueError:
            pass
    print(f"  [{label}] HTTP {r.status_code}  rate-limit: {rem}/{lim} (reset {reset_str})")


def get(auth: OAuth1, path: str, params: dict) -> requests.Response:
    r = requests.get(f"{API}{path}", params=params, auth=auth, timeout=30)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5, help="ホームTLの最大ページ数(既定5)")
    ap.add_argument("--max-results", type=int, default=100, help="1ページの件数(既定100)")
    ap.add_argument("--list-id", default=None, help="厳選リストの1ページ小確認(任意)")
    ap.add_argument("--yes", action="store_true", help="課金確認プロンプトをスキップ")
    args = ap.parse_args()

    env = load_env(REPO_ROOT / ".env")
    auth = make_auth(env)

    max_reads = args.pages * args.max_results + (args.max_results if args.list_id else 0)
    print(f"最大読取 {max_reads} 件($0.005/件なら ~${max_reads * 0.005:.2f})。実行前に課金メーターを控えること。")
    if not args.yes:
        if input("続行する? [y/N] ").strip().lower() != "y":
            sys.exit("中止")

    # 自分の user id
    r = get(auth, "/users/me", {})
    show_rate_limit("users/me", r)
    if r.status_code != 200:
        sys.exit(f"ERROR: /users/me 失敗: {r.status_code} {r.text[:300]}")
    me = r.json()["data"]
    print(f"認証OK: @{me['username']} (id={me['id']})")

    # 逆時系列ホームTL
    tweets: list[dict] = []
    users: dict[str, str] = {}  # author_id -> username
    total_reads = 1  # users/me も Posts Read には数えない想定だが、リクエスト数として記録
    next_token = None
    for page in range(1, args.pages + 1):
        params = {
            "max_results": args.max_results,
            "tweet.fields": TWEET_FIELDS,
            "expansions": EXPANSIONS,
        }
        if next_token:
            params["pagination_token"] = next_token
        r = get(auth, f"/users/{me['id']}/timelines/reverse_chronological", params)
        show_rate_limit(f"home page {page}", r)
        if r.status_code == 429:
            print("  rate limit 到達。打ち切り")
            break
        if r.status_code != 200:
            print(f"  ERROR: {r.status_code} {r.text[:300]} — 打ち切り")
            break
        body = r.json()
        data = body.get("data", [])
        for u in body.get("includes", {}).get("users", []):
            users[u["id"]] = u["username"]
        tweets.extend(data)
        total_reads += len(data)
        if data:
            print(f"  {len(data)}件  {data[-1]['created_at']} 〜 {data[0]['created_at']}")
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            print("  next_token なし(TL 末尾)")
            break

    if not tweets:
        sys.exit("ERROR: 1件も取得できなかった")

    # 単調性(新→旧)チェック
    ts = [t["created_at"] for t in tweets]
    monotonic = all(ts[i] >= ts[i + 1] for i in range(len(ts) - 1))
    print()
    print(f"=== ホームTL 実測サマリ ===")
    print(f"総取得: {len(tweets)}件 / created_at 範囲: {min(ts)} 〜 {max(ts)}")
    print(f"単調性(新→旧): {'OK' if monotonic else 'NG(順序乱れあり)'}")

    # RT / リプライの内訳(設計 02 §2 の正規化対象の規模感)
    n_rt = sum(1 for t in tweets
               if any(ref.get("type") == "retweeted" for ref in t.get("referenced_tweets", [])))
    n_reply = sum(1 for t in tweets if t.get("in_reply_to_user_id"))
    print(f"内訳: RT {n_rt}件 / リプライ {n_reply}件 / その他 {len(tweets) - n_rt - n_reply}件")

    # CSV 出力(API 由来の文字列は spreadsheet_safe を通す。数値の列はそのまま。GH#246)
    OUT_DIR.mkdir(exist_ok=True)
    with (OUT_DIR / "items.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tweet_id", "created_at", "author_id", "username", "is_rt", "is_reply", "text"])
        for t in tweets:
            is_rt = any(ref.get("type") == "retweeted" for ref in t.get("referenced_tweets", []))
            w.writerow([spreadsheet_safe(t["id"]), spreadsheet_safe(t["created_at"]),
                        spreadsheet_safe(t["author_id"]),
                        spreadsheet_safe(users.get(t["author_id"], "?")), int(is_rt),
                        int(bool(t.get("in_reply_to_user_id"))),
                        spreadsheet_safe(t.get("text", "").replace("\n", " "))])
    counts: dict[str, int] = {}
    for t in tweets:
        name = users.get(t["author_id"], t["author_id"])
        counts[name] = counts.get(name, 0) + 1
    with (OUT_DIR / "authors.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["username", "count"])
        for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            w.writerow([spreadsheet_safe(name), c])
    print(f"CSV: {OUT_DIR / 'items.csv'} / {OUT_DIR / 'authors.csv'}(ノイズ率は authors.csv を目視分類)")

    # リスト方式の小確認(任意)
    if args.list_id:
        print()
        print(f"=== リスト {args.list_id} の1ページ小確認 ===")
        r = get(auth, f"/lists/{args.list_id}/tweets",
                {"max_results": args.max_results, "tweet.fields": "created_at,author_id"})
        show_rate_limit("list page 1", r)
        if r.status_code == 200:
            n = len(r.json().get("data", []))
            total_reads += n
            print(f"  取得 {n}件 — 課金メーターでホームTLと同一カウントか突合すること")
        else:
            print(f"  ERROR: {r.status_code} {r.text[:300]}")

    # コスト早見
    print()
    print(f"=== コスト早見(総読取 {total_reads - 1}件・実効単価は課金メーター突合で確定)===")
    for unit in (0.001, 0.005):
        cap = int(20 / (unit * 30))
        print(f"  単価 ${unit}/件: 今回 ~${(total_reads - 1) * unit:.2f} / "
              f"月$20 に収まる daily_read_cap ≦ {cap}件")
    print("合否判定: 実効単価 × daily_read_cap × 30日 ≦ $20")


if __name__ == "__main__":
    main()
