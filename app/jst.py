"""日本時間(JST)。暦日の計算に複数の段が使う共通の部品(設計 00 §3。GH#113)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def today_jst() -> str:
    return datetime.now(UTC).astimezone(JST).strftime("%Y-%m-%d")
