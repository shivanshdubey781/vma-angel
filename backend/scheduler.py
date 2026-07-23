import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .database import get_db
from .algo.ohlc_source import fetch_candles_for_interval
from .algo.executor import execute_trade, get_live_ltp, verify_order_status
from .algo.angel_broker import place_angelone_order
from .algo.instruments import get_token
from .config import ANGEL_CLIENT_ID

logger = logging.getLogger("vma.scheduler")

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_time():
    return datetime.now(IST).replace(tzinfo=None)

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


_last_position_check = {}  # {trade_id: last_check_timestamp}

async def monitor_open_trades(db, config: dict):
    open_trades = await db.trades.find({"status": "OPEN"}).to_list(length=100)
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

        # SANITY GUARD: If entry_price is <= 1.0, this is a ghost trade from an LTP=0 bug.
        if entry_price <= 1.0:
            logger.error(f"[MONITOR] ❌ Ghost trade detected: {symbol} entry=₹{entry_price}. Auto-closing as INVALID.")
            await db.trades.update_one(
                {"_id": trade["_id"]},
                {"$set": {"status": "CLOSED", "exit_time": get_ist_time(), "exit_price": 0, "close_reason": "INVALID: Ghost trade (entry <= ₹1.0)"}}
            )
            continue

        # ── BROKER POSITION SYNC ──
        # Check every ~60 seconds if the position still exists on the broker side
        trade_id = trade.get("trade_id", str(trade["_id"]))
        order_id = trade.get("order_id", "")
        is_real_trade = (
            order_id
            and not str(order_id).startswith("FAKE_ORDER_")
            and trade.get("broker_verification") == "FILLED"
        )

        if is_real_trade:
            now = datetime.now(IST)
            last_check = _last_position_check.get(trade_id)
            if last_check is None or (now - last_check).total_seconds() >= 5:
                _last_position_check[trade_id] = now
                try:
                    from .algo.executor import check_broker_position
                    pos_result = await check_broker_position(client_id, symbol, qty)
                    if not pos_result["has_position"]:
                        logger.info(f"[MONITOR] 🔄 BROKER SYNC: Position for {symbol} no longer exists on broker side ({pos_result['details']}). Auto-closing in DB.")
                        ltp = await get_live_ltp(client_id, "NFO", symbol)
                        exit_price = ltp if ltp > 0 else entry_price
                        await db.trades.update_one(
                            {"_id": trade["_id"]},
                            {"$set": {
                                "status": "CLOSED",
                                "exit_time": get_ist_time(),
                                "exit_price": exit_price,
                                "close_reason": "BROKER EXIT: Position closed from broker dashboard"
                            }}
                        )
                        _last_position_check.pop(trade_id, None)
                        continue
                except Exception as e:
                    logger.error(f"[MONITOR] Error checking broker position for {symbol}: {e}", exc_info=True)

        direction = trade.get("direction", "BUY")

        highest_price = float(trade.get("highest_price", entry_price))

        ltp = await get_live_ltp(client_id, "NFO", symbol)
        if ltp <= 0:
            continue

        # Track highest price since entry
        price_update = {"current_ltp": ltp}
        if ltp > highest_price:
            highest_price = ltp
            price_update["highest_price"] = highest_price
        await db.trades.update_one({"_id": trade["_id"]}, {"$set": price_update})

        close_reason = None
        trail_active = trade.get("trail_active", False)

        # ── BUY/Option trade logic (applied universally for options) ──
        pnl_pts = ltp - entry_price
        current_sl = entry_price - sl_pts
        target_price = entry_price + target_pts

        if (highest_price - entry_price >= trail_trigger or ltp - entry_price >= trail_trigger) and trail_trigger > 0 and trail_pts > 0:
            trail_active = True

        if trail_active:
            dynamic_sl = entry_price + trail_pts
            if dynamic_sl > current_sl:
                current_sl = dynamic_sl

        if ltp >= target_price:
            close_reason = f"TARGET HIT (+{pnl_pts:.2f})"
        elif ltp <= current_sl:
            if trail_active:
                close_reason = f"TRAIL SL HIT (Locked SL: {current_sl:.2f})"
            else:
                close_reason = f"SL HIT ({pnl_pts:.2f})"

        # Update UI info
        await db.trades.update_one({"_id": trade["_id"]}, {"$set": {
            "trail_active": trail_active,
            "current_sl": current_sl,
            "target_price": target_price
        }})

        if close_reason:
            logger.info(f"Closing trade {symbol} due to {close_reason}")

            res = None
            broker_order_success = True
            if is_real_trade:
                # Before placing exit order, check if position still exists on broker
                try:
                    from .algo.executor import check_broker_position
                    pos_result = await check_broker_position(client_id, symbol, qty)
                    if not pos_result["has_position"]:
                        logger.info(f"[MONITOR] Position for {symbol} already closed on broker side. Skipping exit order.")
                        await db.trades.update_one(
                            {"_id": trade["_id"]},
                            {"$set": {"status": "CLOSED", "exit_time": get_ist_time(), "exit_price": ltp, "close_reason": close_reason + " (already exited on broker)"}}
                        )
                        _last_position_check.pop(trade_id, None)
                        continue
                except Exception as e:
                    logger.error(f"[MONITOR] Error checking position before exit: {e}")

                token = get_token(symbol)
                if token:
                    # Use aggressive LIMIT price (5% below LTP for SELL) to guarantee fill
                    # MasterTrust rejects MARKET orders with price=0 for NFO options
                    aggressive_exit_price = round(round((ltp * 0.95) / 0.05) * 0.05, 2)
                    if aggressive_exit_price <= 0:
                        aggressive_exit_price = 0.05
                    logger.info(f"[MONITOR] Placing exit SELL for {symbol}: LTP={ltp}, aggressive_price={aggressive_exit_price}")
                    res = await place_angelone_order(
                        client_id=client_id,
                        exchange="NFO",
                        token=token,
                        trading_symbol=symbol,
                        side="SELL",
                        qty=qty,
                        price=aggressive_exit_price,
                        product="INTRADAY",
                        order_type="LIMIT",
                    )
                    if res.get("status") != "success":
                        broker_order_success = False
                        logger.error(f"[MONITOR] ❌ Failed to place broker exit order for {symbol}: {res.get('message')}")
                        await db.trades.update_one(
                            {"_id": trade["_id"]},
                            {"$set": {"broker_exit_error": res.get("message"), "last_exit_attempt": get_ist_time()}}
                        )
                    else:
                        # ── VERIFY EXIT ORDER WAS FILLED ──
                        exit_order_id = res.get("order_id")
                        if exit_order_id:
                            logger.info(f"[MONITOR] Exit order placed for {symbol}. OrderID={exit_order_id}. Verifying fill...")
                            verification = await verify_order_status(client_id, exit_order_id, trading_symbol=symbol, max_retries=5)
                            v_status = verification.get("status", "UNKNOWN")
                            logger.info(f"[MONITOR] Exit order {exit_order_id} verification: {v_status} — {verification.get('reason')}")

                            if v_status == "FILLED":
                                # Order confirmed filled — mark trade CLOSED with actual fill price
                                actual_exit_price = verification.get("avg_price", ltp) or ltp
                                await db.trades.update_one(
                                    {"_id": trade["_id"]},
                                    {"$set": {
                                        "status": "CLOSED",
                                        "exit_time": get_ist_time(),
                                        "exit_price": actual_exit_price,
                                        "close_reason": close_reason,
                                        "exit_order_id": exit_order_id,
                                        "broker_verification": "FILLED"
                                    }}
                                )
                                _last_position_check.pop(trade_id, None)
                                logger.info(f"[MONITOR] ✅ Trade {symbol} CLOSED confirmed at ₹{actual_exit_price:.2f}")
                            elif v_status == "REJECTED":
                                # Order was rejected — retry with fresh LTP and aggressive LIMIT
                                logger.warning(f"[MONITOR] ⚠️ Exit order {exit_order_id} REJECTED: {verification.get('reason')}. Retrying with fresh LTP...")
                                fresh_ltp = await get_live_ltp(client_id, "NFO", symbol)
                                if fresh_ltp <= 0:
                                    fresh_ltp = ltp
                                retry_price = round(round((fresh_ltp * 0.90) / 0.05) * 0.05, 2)
                                if retry_price <= 0:
                                    retry_price = 0.05
                                retry_res = await place_angelone_order(
                                    client_id=client_id, exchange="NFO", token=token,
                                    trading_symbol=symbol, side="SELL", qty=qty,
                                    price=retry_price, product="INTRADAY", order_type="LIMIT",
                                )
                                if retry_res.get("status") == "success":
                                    retry_order_id = retry_res.get("order_id")
                                    retry_verify = await verify_order_status(client_id, retry_order_id, trading_symbol=symbol, max_retries=5)
                                    rv_status = retry_verify.get("status", "UNKNOWN")
                                    if rv_status == "FILLED":
                                        actual_exit_price = retry_verify.get("avg_price", ltp) or ltp
                                        await db.trades.update_one(
                                            {"_id": trade["_id"]},
                                            {"$set": {
                                                "status": "CLOSED",
                                                "exit_time": get_ist_time(),
                                                "exit_price": actual_exit_price,
                                                "close_reason": close_reason,
                                                "exit_order_id": retry_order_id,
                                                "broker_verification": "FILLED (retry)"
                                            }}
                                        )
                                        _last_position_check.pop(trade_id, None)
                                        logger.info(f"[MONITOR] ✅ Retry exit FILLED at ₹{actual_exit_price:.2f}")
                                    else:
                                        logger.error(f"[MONITOR] ❌ Retry exit also {rv_status}. Trade left OPEN for next cycle.")
                                        await db.trades.update_one(
                                            {"_id": trade["_id"]},
                                            {"$set": {"broker_exit_error": f"Retry {rv_status}: {retry_verify.get('reason')}", "last_exit_attempt": get_ist_time()}}
                                        )
                                else:
                                    logger.error(f"[MONITOR] ❌ Retry order failed: {retry_res.get('message')}")
                                    await db.trades.update_one(
                                        {"_id": trade["_id"]},
                                        {"$set": {"broker_exit_error": f"Retry failed: {retry_res.get('message')}", "last_exit_attempt": get_ist_time()}}
                                    )
                            else:
                                # PENDING or UNKNOWN — do NOT mark closed, leave for next monitor cycle
                                logger.warning(f"[MONITOR] ⏳ Exit order {exit_order_id} status={v_status}. NOT marking closed. Will retry next cycle.")
                                await db.trades.update_one(
                                    {"_id": trade["_id"]},
                                    {"$set": {"pending_exit_order_id": exit_order_id, "last_exit_attempt": get_ist_time(), "broker_exit_error": f"Exit {v_status}: {verification.get('reason')}"}}
                                )
                        else:
                            logger.error(f"[MONITOR] ❌ No order_id returned from broker for {symbol}. Trade left OPEN.")
                            broker_order_success = False
                else:
                    logger.error(f"[MONITOR] ❌ Token not found for {symbol} to place exit order!")
                    broker_order_success = False
            else:
                logger.info(f"[MONITOR] Simulated/Fake trade exit for {symbol}. Skipping broker order submission.")
                # For simulated trades, mark closed immediately
                await db.trades.update_one(
                    {"_id": trade["_id"]},
                    {"$set": {
                        "status": "CLOSED",
                        "exit_time": get_ist_time(),
                        "exit_price": ltp,
                        "close_reason": close_reason
                    }}
                )
                _last_position_check.pop(trade_id, None)


# ──────────────────────────────────────────────────────────
#  MAIN SCHEDULER LOOP
# ──────────────────────────────────────────────────────────

async def run_scheduler():
    logger.info("Scheduler started.")
    db = get_db()

    # Track the last signal direction that was acted on to prevent duplicate fires.
    # A new trade only fires when the VMA crossover direction CHANGES.
    _last_executed_signal = "NONE"
    try:
        last_trade = await db.trades.find_one({}, sort=[("entry_time", -1)])
        if last_trade:
            _last_executed_signal = "CE" if last_trade.get("direction") == "BUY" else "PE"
            logger.info(f"[SCHED] Initialized _last_executed_signal from database: {_last_executed_signal} (last trade was {last_trade.get('direction')})")
    except Exception as e:
        logger.error(f"[SCHED] Error initializing _last_executed_signal from database: {e}")

    # ── Startup: Sync signals history database with clean candle data ──
    try:
        import pymongo
        logger.info("[SCHED] Startup: Syncing signals history database with clean historical candles...")
        config = await db.config.find_one({"_id": "vma_config"})
        if config:
            current_interval = config.get("interval", "FIVE_MINUTE")
            rows = await fetch_candles_for_interval(current_interval, 2000, drop_live=False)
            if rows:
                from .algo.vma import compute_dual_vma
                all_bars = compute_dual_vma(rows, config.get("fast_period", 5), config.get("slow_period", 9))
                
                # Delete all existing tick-level duplicates
                await db.signals.delete_many({})
                
                operations = []
                for bar in all_bars:
                    if not bar.get("timestamp"):
                        continue
                    try:
                        c_ts = datetime.strptime(bar["timestamp"], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        try:
                            c_ts = datetime.fromisoformat(bar["timestamp"])
                        except Exception:
                            continue
                    
                    sig = bar.get("signal", "NONE")
                    t_dir = "BUY" if sig == "CE" else "SELL" if sig == "PE" else "NONE"
                    
                    operations.append(
                        pymongo.UpdateOne(
                            {"timestamp": c_ts},
                            {"$set": {
                                "open": bar.get("open"),
                                "high": bar.get("high"),
                                "low": bar.get("low"),
                                "close": bar.get("close"),
                                "short_vma": bar.get("short_vma"),
                                "long_vma": bar.get("long_vma"),
                                "fast_vma": bar["short_vma"],
                                "slow_vma": bar["long_vma"],
                                "signal": bar.get("signal", "NONE"),
                                "confirm_signal": bar.get("confirm_signal", "NONE"),
                                "used_signal": t_dir,
                                "svma_trend": bar.get("svma_trend", "FLAT"),
                                "position": bar.get("position", "CROSS"),
                                "quality": bar.get("quality", 0),
                                "is_sideways": bar.get("is_sideways", False),
                                "atr": bar.get("atr"),
                                "rsi": bar.get("rsi"),
                                "skip_reason": "NONE"
                            }},
                            upsert=True
                        )
                    )
                if operations:
                    await db.signals.bulk_write(operations)
                    logger.info(f"[SCHED] Startup sync complete. Wrote {len(operations)} clean historical signals.")
    except Exception as e:
        logger.error(f"[SCHED] Error during startup signals sync: {e}", exc_info=True)

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

            # ── Step 1: Feed live OHLC data ──
            spot_ltp = await fetch_nifty_spot_ltp()
            if spot_ltp > 0:
                await update_ohlc_candles(db, spot_ltp)
                logger.info(f"[FEEDER] Nifty spot LTP = {spot_ltp:.2f}")
            else:
                logger.warning("[FEEDER] Could not fetch Nifty spot LTP!")

            # ── Step 2: Fetch config ──
            config = await db.config.find_one({"_id": "vma_config"})
            if not config:
                logger.warning("[SCHED] No config found. Sleeping 5s.")
                await asyncio.sleep(5)
                continue

            # ── Step 3: Monitor open trades ──
            await monitor_open_trades(db, config)

            # ── Step 4: Compute VMA and detect crossovers ──
            current_interval = config.get("interval", "FIVE_MINUTE")
            rows = await fetch_candles_for_interval(current_interval, 2000, drop_live=False)

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

            # Allow configurable immediate entry on current-bar crossover.
            # By default the system uses the previous-bar 'confirm_signal' (safer).
            immediate_entry = bool(config.get("immediate_entry", False))
            signal_source = "signal" if immediate_entry else "confirm_signal"
            
            forced_signal = config.get("force_signal")
            is_forced = False
            if forced_signal in ["CE", "PE"]:
                last_signal_ce_pe = forced_signal
                is_forced = True
                await db.config.update_one({"_id": "vma_config"}, {"$unset": {"force_signal": ""}})
                logger.info(f"[SCHED] ⚠️ FORCED SIGNAL OVERRIDE DETECTED: {forced_signal}. Cleared override in DB.")
            else:
                last_signal_ce_pe = last_bar.get(signal_source, "NONE")

            if last_signal_ce_pe == "CE":
                trade_dir = "BUY"
            elif last_signal_ce_pe == "PE":
                trade_dir = "SELL"
            else:
                trade_dir = "NONE"

            logger.info(
                f"[SCHED] VMA: signal={last_bar['signal']}, confirm={last_bar['confirm_signal']}, "
                f"dir={trade_dir}, forced={is_forced}, quality={last_bar['quality']}, "
                f"svma={last_bar['short_vma']:.4f}, lvma={last_bar['long_vma']:.4f}, "
                f"close={last_bar['close']}, sideways={last_bar['is_sideways']}"
            )

            # Parse candle timestamp string to datetime object to avoid duplicate logs per candle
            candle_ts = None
            if "timestamp" in last_bar and last_bar["timestamp"]:
                try:
                    candle_ts = datetime.strptime(last_bar["timestamp"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    try:
                        candle_ts = datetime.fromisoformat(last_bar["timestamp"])
                    except Exception:
                        pass
            
            if not candle_ts:
                candle_ts = get_ist_time()

            await db.signals.update_one(
                {"timestamp": candle_ts},
                {"$set": {
                    "open": last_bar.get("open"),
                    "high": last_bar.get("high"),
                    "low": last_bar.get("low"),
                    "close": last_bar.get("close"),
                    "short_vma": last_bar.get("short_vma"),
                    "long_vma": last_bar.get("long_vma"),
                    "fast_vma": last_bar["short_vma"],
                    "slow_vma": last_bar["long_vma"],
                    # store both immediate and confirm signals for debugging/inspection
                    "signal": last_bar.get("signal", "NONE"),
                    "confirm_signal": last_bar.get("confirm_signal", "NONE"),
                    "used_signal": trade_dir,
                    "svma_trend": last_bar.get("svma_trend", "FLAT"),
                    "position": last_bar.get("position", "CROSS"),
                    "quality": last_bar["quality"],
                    "is_sideways": last_bar["is_sideways"],
                    "atr": last_bar.get("atr"),
                    "rsi": last_bar.get("rsi"),
                    "skip_reason": "FORCED" if is_forced else "NONE"
                }},
                upsert=True
            )

            # ── Step 5: Execute trade if NEW signal crossover detected ──
            if config.get("algo_enabled", False):
                # When using immediate_entry, the 'quality' in last_bar was computed
                # with confirm_signal semantics. Allow 2 as minimum if immediate.
                effective_quality = last_bar["quality"]
                # REMOVED: Do NOT artificially boost quality for immediate_entry
                # effective_quality = max(effective_quality, 2)  # DISABLED: quality must be real >= 2

                if trade_dir in ["BUY", "SELL"] and (effective_quality >= 3 or is_forced):
                    if is_forced or trade_dir != _last_executed_signal:
                        logger.info(f"[SCHED] *** NEW CROSSOVER {'(FORCED)' if is_forced else ''} *** {_last_executed_signal} → {trade_dir} (Quality {effective_quality}) - source={signal_source}")
                        _last_executed_signal = trade_dir
                        result = await execute_trade(trade_dir, last_bar["close"], config)
                        if result:
                            logger.info(f"[SCHED] Trade executed successfully. Tracking signal = {trade_dir}")
                        else:
                            logger.warning(f"[SCHED] execute_trade did not open a trade (cooldown/active trade/LTP error). Crossover marked as consumed.")
                    else:
                        logger.info(f"[SCHED] Same signal {trade_dir} still active — no new crossover. Skipping.")
                else:
                    logger.info(f"[SCHED] No actionable signal. dir={trade_dir}, quality={last_bar['quality']}")
                    if trade_dir == "NONE":
                        _last_executed_signal = "NONE"  # Reset when signal clears
            else:
                logger.info("[SCHED] Algo DISABLED. Skipping trade.")

        except Exception as e:
            logger.error(f"[SCHED] Error: {e}", exc_info=True)

        await asyncio.sleep(5)


async def run_auto_token_generator():
    """
    Background worker that runs indefinitely.
    Checks the current IST time every 30 seconds.
    On weekdays (Mon-Fri) at 09:10 AM IST, it triggers AngelOne TOTP auto-login.
    """
    logger.info("[AUTO-LOGIN] AngelOne auto-login worker initialized.")
    last_run_date = None

    while True:
        try:
            now_ist = datetime.now(IST)

            # Weekdays (Monday=0 to Friday=4)
            if now_ist.weekday() < 5:
                current_date = now_ist.date()

                # Trigger between 09:10 AM and 09:15 AM IST
                if now_ist.hour == 9 and 10 <= now_ist.minute <= 15:
                    if last_run_date != current_date:
                        logger.info(
                            f"[AUTO-LOGIN] Triggering AngelOne TOTP login at 09:10 AM IST for {current_date}..."
                        )
                        from .auth.angel_auth import run_auto_login
                        # Run synchronous login in thread pool
                        res = await asyncio.to_thread(run_auto_login)
                        logger.info(f"[AUTO-LOGIN] Response: {res}")
                        last_run_date = current_date

        except Exception as e:
            logger.error(f"[AUTO-LOGIN] Error in auto-login worker: {e}", exc_info=True)

        await asyncio.sleep(30)

