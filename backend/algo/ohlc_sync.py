import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("vma.ohlc_sync")

IST = timezone(timedelta(hours=5, minutes=30))

async def fetch_yahoo_ohlc(interval: str):
    m = {
        "1min": "1m",
        "5min": "5m"
    }
    yf_interval = m.get(interval, "1m") # fallback to 1m for resampling
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/^NSEI?range=7d&interval={yf_interval}"
    
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            return []
            
        data = res.json()
        result = data.get("chart", {}).get("result", [])
        if not result: return []
        
        timestamps = result[0].get("timestamp", [])
        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        
        rows = []
        for i in range(len(timestamps)):
            if opens[i] is None: continue 
            dt = datetime.fromtimestamp(timestamps[i], tz=IST)
            
            rows.append({
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i])
            })
            
        if interval == "3min":
            # Resample 1m to 3m aligned to market-open (9:15 IST) boundaries,
            # matching TradingView's candle grouping exactly.
            from collections import defaultdict
            buckets: dict = defaultdict(list)
            MARKET_OPEN_MINUTE = 9 * 60 + 15  # 555 minutes from midnight
            for row in rows:
                try:
                    dt = datetime.fromisoformat(row["timestamp"])
                    minute_of_day = dt.hour * 60 + dt.minute
                    offset = minute_of_day - MARKET_OPEN_MINUTE
                    # Group into 3-minute aligned buckets
                    bucket_idx = offset // 3
                    bucket_start_minute = MARKET_OPEN_MINUTE + bucket_idx * 3
                    bucket_dt = dt.replace(
                        hour=bucket_start_minute // 60,
                        minute=bucket_start_minute % 60,
                        second=0,
                        microsecond=0,
                    )
                    buckets[bucket_dt].append(row)
                except Exception:
                    continue

            resampled = []
            for bucket_dt in sorted(buckets.keys()):
                chunk = buckets[bucket_dt]
                resampled.append({
                    "timestamp": bucket_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": chunk[0]["open"],
                    "high": max(c["high"] for c in chunk),
                    "low": min(c["low"] for c in chunk),
                    "close": chunk[-1]["close"],
                })
            return resampled
            
        return rows

async def sync_ohlc_to_mongo(db: AsyncIOMotorDatabase):
    collections_map = {
        "1min": "OHLC",
        "3min": "OHLC3",
        "5min": "OHLC5"
    }
    
    for timeframe, coll_name in collections_map.items():
        try:
            rows = await fetch_yahoo_ohlc(timeframe)
            if not rows: continue
            
            col = db[coll_name]
            from pymongo import UpdateOne
            operations = []
            for r in rows:
                operations.append(UpdateOne({"timestamp": r["timestamp"]}, {"$set": r}, upsert=True))
            
            if operations:
                await col.bulk_write(operations, ordered=False)
                logger.info(f"Synced {len(operations)} bars to {coll_name} ({timeframe})")
                
        except Exception as e:
            logger.error(f"Error syncing {timeframe}: {e}")

async def start_ohlc_sync_loop(db: AsyncIOMotorDatabase):
    while True:
        await sync_ohlc_to_mongo(db)
        await asyncio.sleep(60) 
