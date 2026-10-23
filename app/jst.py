"""日本時間(JST)。暦日の計算に複数の段が使う共通の部品(設計 00 §3。GH#113)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    return datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d")


def jst_day_bounds_utc(day: str) -> tuple[str, str]:
    """JST 暦日 day(YYYY-MM-DD)の [0:00, 翌 0:00) を UTC の ISO 文字列で返す(analyzed_at の範囲)。

    その日に分析された分を見る段(トレンド・digest の全滅判定。GH#175)が同じ境界を使う。
    """
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=JST)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start.astimezone(UTC).strftime(fmt),
        (start + timedelta(days=1)).astimezone(UTC).strftime(fmt),
    )
