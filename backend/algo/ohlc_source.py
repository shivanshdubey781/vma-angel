from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from ..config import (
    OHLC_MONGO_DB,
    OHLC_MONGO_COLLECTION_1MIN,
    OHLC_MONGO_COLLECTION_3MIN,
    OHLC_MONGO_COLLECTION_5MIN,
    OHLC_MONGO_URI,
)


@dataclass(frozen=True)
class OhlcSourceConfig:
    uri: str
    db_name: str
    collection: str


INTERVAL_SOURCE_MAP = {
    "ONE_MINUTE": OhlcSourceConfig(
        uri=OHLC_MONGO_URI,
        db_name=OHLC_MONGO_DB,
        collection=OHLC_MONGO_COLLECTION_1MIN,
    ),
    "THREE_MINUTE": OhlcSourceConfig(
        uri=OHLC_MONGO_URI,
        db_name=OHLC_MONGO_DB,
        collection=OHLC_MONGO_COLLECTION_3MIN,
    ),
    "FIVE_MINUTE": OhlcSourceConfig(
        uri=OHLC_MONGO_URI,
        db_name=OHLC_MONGO_DB,
        collection=OHLC_MONGO_COLLECTION_5MIN,
    ),
}

_TIME_FIELDS = ["timestamp_ist", "timestamp", "datetime", "date", "time", "ts", "t", "open_time"]
_CLIENT_CACHE: dict[str, AsyncIOMotorClient] = {}


def get_source_config(interval: str) -> OhlcSourceConfig:
    return INTERVAL_SOURCE_MAP.get(interval, INTERVAL_SOURCE_MAP["FIVE_MINUTE"])


def _get_client(uri: str) -> AsyncIOMotorClient:
    client = _CLIENT_CACHE.get(uri)
    if client is None:
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        _CLIENT_CACHE[uri] = client
    return client


def _normalize_rows(docs: list[dict]) -> list[dict]:
    rows = []
    seen = set()

    for doc in docs:
        norm = {k.lower(): v for k, v in doc.items()}
        ts = ""
        for field in _TIME_FIELDS:
            if norm.get(field):
                ts = str(norm[field])
                break
        if not ts or ts in seen:
            continue
        seen.add(ts)
        rows.append(
            {
                "timestamp": ts,
                "open": float(norm.get("open", 0) or 0),
                "high": float(norm.get("high", 0) or 0),
                "low": float(norm.get("low", 0) or 0),
                "close": float(norm.get("close", 0) or 0),
            }
        )

    return rows


_INTERVAL_MINUTES = {
    "ONE_MINUTE": 1,
    "THREE_MINUTE": 3,
    "FIVE_MINUTE": 5,
}


async def fetch_candles_for_interval(interval: str, limit: int = 2000, drop_live: bool = True) -> list[dict]:
    source = get_source_config(interval)
    client = _get_client(source.uri)
    collection = client[source.db_name][source.collection]

    # Dynamically find the correct timestamp field for sorting
    sample = await collection.find_one()
    ts_field = "timestamp"
    if sample:
        key_map = {k.lower(): k for k in sample.keys()}
        for f in _TIME_FIELDS:
            if f in key_map:
                ts_field = key_map[f]
                break

    # Fetch one extra so that after dropping the live candle we still have `limit` bars
    fetch_limit = limit + 1 if drop_live else limit
    docs = await collection.find({}, {"_id": 0}).sort(ts_field, -1).limit(fetch_limit).to_list(length=fetch_limit)
    if not docs:
        docs = await collection.find({}, {"_id": 0}).sort("$natural", -1).limit(fetch_limit).to_list(length=fetch_limit)

    docs.reverse()
    rows = _normalize_rows(docs)

    # Drop the last (currently-forming) candle so VMA matches TradingView's
    # closed-bar calculation. We compare the last candle's timestamp against
    # the current time; if the candle opened less than interval_minutes ago
    # it is still live.
    if drop_live and rows:
        interval_minutes = _INTERVAL_MINUTES.get(interval, 5)
        try:
            from datetime import datetime, timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST)
            last_ts_str = rows[-1]["timestamp"]
            # Support both datetime objects and ISO-format strings
            if isinstance(last_ts_str, str):
                last_dt = datetime.fromisoformat(last_ts_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=IST)
            else:
                last_dt = last_ts_str
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=IST)
            age_seconds = (now_ist - last_dt).total_seconds()
            # If the last candle is younger than one interval period, it's live — drop it
            if 0 <= age_seconds < interval_minutes * 60:
                rows = rows[:-1]
        except Exception:
            # If timestamp parsing fails, conservatively drop the last candle
            rows = rows[:-1]

    return rows


async def fetch_latest_close(interval: str) -> Optional[float]:
    rows = await fetch_candles_for_interval(interval, limit=1)
    if not rows:
        return None
    close = rows[-1].get("close")
    return float(close) if close is not None else None
