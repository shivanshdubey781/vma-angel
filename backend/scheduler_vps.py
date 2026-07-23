import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .database import get_db
from .algo.executor import execute_trade, get_live_ltp
from .algo.angel_broker import place_angelone_order
from .algo.instruments import get_token
from .config import ANGEL_CLIENT_ID

logger = logging.getLogger("vma.scheduler")

IST = timezone(timedelta(hours=5, minutes=30))

# ──────────────────────────────────────────────────────────
#  LIVE OHLC DATA FEEDER
#  Fetches Nifty 50 spot LTP and builds 1/3/5 min candles
# ──────────────────────────────────────────────────────────

# In-memory candle builders  {interval_minutes: {open, high, low, close, start_time}}
_candle_state = {}

def _get_candle_start(now_ist: datetime, interval_min: int) -> datetime:
    """Round down to the start of the current candle window."""
    total_min = now_ist.hour * 60 + now_ist.minute
    floored = (total_min // interval_min) * interval_min
    return now_ist.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)

async def fetch_nifty_spot_ltp() -> float:
    """Fetch Nifty 50 spot LTP from NSE public API (no auth required)."""
    import httpx
    try:
        url = "https://www.nseindia.com/api/marketStatus"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                for m in data.get("marketState", []):
                    if m.get("index") == "NIFTY 50":
                        ltp = float(m.get("last", 0))
                        if ltp > 0:
                            return ltp
                logger.warning("[FEEDER] NIFTY 50 not found in NSE marketStatus response.")
            else:
                logger.warning(f"[FEEDER] NSE API returned {res.status_code}")
    except Exception as e:
        logger.error(f"[FEEDER] Error fetching Nifty spot from NSE: {e}")
    return 0.0


async def update_ohlc_candles(db, ltp: float):
    """
    Feed the live LTP into 1-min, 3-min, and 5-min candle builders.
    When a candle window closes, write it to MongoDB.
    """
    global _candle_state
    now_ist = datetime.now(IST)

    INTERVALS = {
        1: "OHLC",
        3: "OHLC3",
        5: "OHLC5",
    }

    for interval_min, col_name in INTERVALS.items():
        candle_start = _get_candle_start(now_ist, interval_min)
        key = interval_min

        if key not in _candle_state:
            _candle_state[key] = {
                "start": candle_start,
                "open": ltp, "high": ltp, "low": ltp, "close": ltp
            }
        
        state = _candle_state[key]

        if candle_start > state["start"]:
            # The previous candle window is complete — save it to MongoDB
            candle_doc = {
                "timestamp": state["start"].strftime("%Y-%m-%d %H:%M:%S"),
                "open": round(state["open"], 2),
                "high": round(state["high"], 2),
                "low": round(state["low"], 2),
                "close": round(state["close"], 2),
            }
            try:
                # Upsert to avoid duplicates
                await db[col_name].update_one(
                    {"timestamp": candle_doc["timestamp"]},
                    {"$set": candle_doc},
                    upsert=True
                )
                logger.info(f"[FEEDER] Saved {interval_min}min candle: {candle_doc['timestamp']} O={candle_doc['open']} H={candle_doc['high']} L={candle_doc['low']} C={candle_doc['close']}")
            except Exception as e:
                logger.error(f"[FEEDER] Error saving {interval_min}min candle: {e}")

            # Start a new candle
            _candle_state[key] = {
                "start": candle_start,
                "open": ltp, "high": ltp, "low": ltp, "close": ltp
            }
        else:
            # Same candle window — update OHLC
            state["high"] = max(state["high"], ltp)
            state["low"] = min(state["low"], ltp)
            state["close"] = ltp


# ──────────────────────────────────────────────────────────
#  OHLC READER (for VMA computation)
# ──────────────────────────────────────────────────────────

async def fetch_real_candles(db, interval: str, limit: int = 2000):
    INTERVAL_TO_COLLECTION = {
        "ONE_MINUTE": "OHLC",
        "THREE_MINUTE": "OHLC3",
        "FIVE_MINUTE": "OHLC5"
    }
    col_name = INTERVAL_TO_COLLECTION.get(interval, "OHLC5")
    col = db[col_name]

    docs = await col.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(length=limit)
    if not docs:
        docs = await col.find({}, {"_id": 0}).sort("$natural", -1).limit(limit).to_list(length=limit)

    docs.reverse()

    rows = []
    seen = set()
    _TIME_FIELDS = ["timestamp", "datetime", "date", "time", "ts", "t", "open_time"]

    for d in docs:
        norm = {k.lower(): v for k, v in d.items()}
        ts = ""
        for f in _TIME_FIELDS:
            if norm.get(f):
                ts = str(norm[f])
                break
        if not ts or ts in seen:
            continue
        seen.add(ts)
        rows.append({
            "timestamp": ts,
            "open": float(norm.get("open", 0) or 0),
            "high": float(norm.get("high", 0) or 0),
            "low": float(norm.get("low", 0) or 0),
            "close": float(norm.get("close", 0) or 0)
        })

    return rows


# ──────────────────────────────────────────────────────────
#  TRADE MONITOR  (SL / TP / Trailing)
# ──────────────────────────────────────────────────────────

async def monitor_open_trades(db, config: dict):
    open_trades = await db.trades.find({"status": "OPEN", "entry_price": {"$gte": 10}}).to_list(length=100)
    if not open_trades:
        return

    client_id = ANGEL_CLIENT_ID

    target_pts = float(config.get("target_pts", 50))
    sl_pts = float(config.get("sl_pts", 30))
    trail_trigger = float(config.get("trail_trigger", 20))
    trail_pts = float(config.get("trail_pts", 15))

    for trade in open_trades:
        symbol = trade["symbol"]
        entry_price = float(trade["entry_price"])
        qty = int(trade["quantity"])

        direction = trade.get("direction", "BUY")
        is_sell = direction == "SELL"

        highest_price = float(trade.get("highest_price", entry_price))
        lowest_price = float(trade.get("lowest_price", entry_price))

        ltp = await get_live_ltp(client_id, "NFO", symbol)
        if ltp <= 0:
            continue

        # Track highest for BUY trailing, lowest for SELL trailing
        price_update = {"current_ltp": ltp}
        if not is_sell:
            if ltp > highest_price:
                highest_price = ltp
                price_update["highest_price"] = highest_price
        else:
            if ltp < lowest_price or lowest_price <= 0:
                lowest_price = ltp
                price_update["lowest_price"] = lowest_price
        await db.trades.update_one({"_id": trade["_id"]}, {"$set": price_update})

        close_reason = None
        trail_active = False

        if not is_sell:
            # ── BUY trade logic ──
            pnl_pts = ltp - entry_price
            current_sl = entry_price - sl_pts
            target_price = entry_price + target_pts

            if highest_price >= entry_price + trail_trigger and trail_trigger > 0 and trail_pts > 0:
                trail_active = True
                dynamic_sl = highest_price - trail_pts
                if dynamic_sl > current_sl:
                    current_sl = dynamic_sl

            if ltp >= target_price:
                close_reason = f"TARGET HIT (+{pnl_pts:.2f})"
            elif ltp <= current_sl:
                if trail_active:
                    close_reason = f"TRAIL SL HIT (High: {highest_price:.2f}, Drop: {highest_price - ltp:.2f})"
                else:
                    close_reason = f"SL HIT ({pnl_pts:.2f})"
        else:
            # ── SELL trade logic ──
            pnl_pts = entry_price - ltp
            current_sl = entry_price + sl_pts
            target_price = entry_price - target_pts

            if lowest_price <= entry_price - trail_trigger and trail_trigger > 0 and trail_pts > 0:
                trail_active = True
                dynamic_sl = lowest_price + trail_pts
                if dynamic_sl < current_sl:
                    current_sl = dynamic_sl

            if ltp <= target_price:
                close_reason = f"TARGET HIT (+{pnl_pts:.2f})"
            elif ltp >= current_sl:
                if trail_active:
                    close_reason = f"TRAIL SL HIT (Low: {lowest_price:.2f}, Rise: {ltp - lowest_price:.2f})"
                else:
                    close_reason = f"SL HIT ({pnl_pts:.2f})"

        # Update UI info
        await db.trades.update_one({"_id": trade["_id"]}, {"$set": {
            "trail_active": trail_active,
            "current_sl": current_sl,
            "target_price": target_price,
        }})

        if close_reason:
            logger.info(f"Closing trade {symbol} due to {close_reason}")
            token = get_token(symbol)
            if token:
                await place_angelone_order(
                    client_id=client_id,
                    exchange="NFO",
                    token=token,
                    trading_symbol=symbol,
                    side="SELL",
                    qty=qty,
                    price=ltp,
                    product="INTRADAY",
                )

            await db.trades.update_one(
                {"_id": trade["_id"]},
                {"$set": {"status": "CLOSED", "exit_time": datetime.utcnow(), "exit_price": ltp, "close_reason": close_reason}}
            )


# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
#  MAIN SCHEDULER LOOP
# ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

async def run_scheduler():
    logger.info("Scheduler started.")
    db = get_db()

    while True:
        try:
            now_ist = datetime.now(IST)
            # Only run during market hours (9:15 - 15:30 IST)
            market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

            if not (market_open <= now_ist <= market_close) or now_ist.weekday() >= 5:
                logger.info(f"[SCHED] Outside market hours or weekend ({now_ist.strftime('%H:%M %a IST')}). Sleeping 60s.")
                await asyncio.sleep(60)
                continue

            logger.info(f"[SCHED] Loop tick at {now_ist.strftime('%H:%M:%S IST')}...")

            # ΓöÇΓöÇ Step 1: Feed live OHLC data ΓöÇΓöÇ
            spot_ltp = await fetch_nifty_spot_ltp()
            if spot_ltp > 0:
                await update_ohlc_candles(db, spot_ltp)
                logger.info(f"[FEEDER] Nifty spot LTP = {spot_ltp:.2f}")
            else:
                logger.warning("[FEEDER] Could not fetch Nifty spot LTP!")

            # ΓöÇΓöÇ Step 2: Fetch config ΓöÇΓöÇ
            config = await db.config.find_one({"_id": "vma_config"})
            if not config:
                logger.warning("[SCHED] No config found. Sleeping 5s.")
                await asyncio.sleep(5)
                continue

            # ΓöÇΓöÇ Step 3: Monitor open trades ΓöÇΓöÇ
            await monitor_open_trades(db, config)

            # ΓöÇΓöÇ Step 4: Compute VMA and detect crossovers ΓöÇΓöÇ
            current_interval = config.get("interval", "FIVE_MINUTE")
            rows = await fetch_real_candles(db, current_interval, 2000)

            if not rows:
                logger.warning(f"[SCHED] No OHLC data in MongoDB for {current_interval}!")
                await asyncio.sleep(10)
                continue

            logger.info(f"[SCHED] Got {len(rows)} OHLC rows. Last: ts={rows[-1]['timestamp']}, close={rows[-1]['close']}")

            from .algo.vma import compute_dual_vma
            all_bars = compute_dual_vma(rows, config.get("fast_period", 5), config.get("slow_period", 9))

            if not all_bars:
                logger.warning("[SCHED] compute_dual_vma returned empty.")
                continue

            last_bar = all_bars[-1]
            last_signal_ce_pe = last_bar["confirm_signal"]

            if last_signal_ce_pe == "CE":
                trade_dir = "BUY"
            elif last_signal_ce_pe == "PE":
                trade_dir = "SELL"
            else:
                trade_dir = "NONE"

            logger.info(
                f"[SCHED] VMA: signal={last_bar['signal']}, confirm={last_bar['confirm_signal']}, "
                f"dir={trade_dir}, quality={last_bar['quality']}, "
                f"svma={last_bar['short_vma']:.4f}, lvma={last_bar['long_vma']:.4f}, "
                f"close={last_bar['close']}, sideways={last_bar['is_sideways']}"
            )

            await db.signals.insert_one({
                "timestamp": datetime.utcnow(),
                "fast_vma": last_bar["short_vma"],
                "slow_vma": last_bar["long_vma"],
                "signal": trade_dir,
                "close": last_bar["close"],
                "quality": last_bar["quality"],
                "is_sideways": last_bar["is_sideways"]
            })

            # ── Step 5: Execute trade if signal detected ──
            if config.get("algo_enabled", False):
                if trade_dir in ["BUY", "SELL"] and last_bar["quality"] >= 2:
                    logger.info(f"[SCHED] *** EXECUTING TRADE *** {trade_dir} (Quality {last_bar['quality']})")
                    await execute_trade(trade_dir, last_bar["close"], config)
                else:
                    logger.info(f"[SCHED] No actionable signal. dir={trade_dir}, quality={last_bar['quality']}")
            else:
                logger.info("[SCHED] Algo DISABLED. Skipping trade.")

        except Exception as e:
            logger.error(f"[SCHED] Error: {e}", exc_info=True)

        await asyncio.sleep(15)

