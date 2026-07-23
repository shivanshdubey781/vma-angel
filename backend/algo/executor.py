import httpx
import asyncio
from datetime import datetime, timedelta, timezone
import uuid
import time
import logging
import os
from ..database import get_db
from ..config import ANGEL_CLIENT_ID

logger = logging.getLogger("vma.executor")

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_time():
    return datetime.now(IST).replace(tzinfo=None)

# ──────────────────────────────────────────────────────────
#  COOLDOWN: Prevent duplicate trades on same signal
# ──────────────────────────────────────────────────────────
_last_trade_info = {"direction": None, "timestamp": None, "symbol": None}
TRADE_COOLDOWN_SECONDS = 120  # 2 min between same-direction trades


def _parse_angelone_symbol(symbol: str):
    """
    Parse AngelOne-format option symbol like NIFTY16JUN2623200CE
    into (underlying, strike, opt_type) tuple.
    Returns None if parsing fails.
    """
    import re
    sym_clean = symbol.replace(" ", "").strip().upper()
    if not (sym_clean.endswith("CE") or sym_clean.endswith("PE")):
        return None
    opt_type = sym_clean[-2:]

    underlying = "NIFTY"
    for index_name in ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]:
        if sym_clean.startswith(index_name):
            underlying = index_name
            break

    # Regex: UNDERLYING + DDMONYY + STRIKE + CE/PE
    pattern = rf'^{underlying}(\d{{2}}[A-Z]{{3}}\d{{2}})(\d+)(CE|PE)$'
    m = re.match(pattern, sym_clean)
    if m:
        return underlying, int(m.group(2)), opt_type

    # Fallback: extract trailing digits before CE/PE
    rem = sym_clean[:-2]
    digits = []
    for char in reversed(rem):
        if char.isdigit():
            digits.append(char)
        else:
            break
    if digits:
        return underlying, int("".join(reversed(digits))), opt_type
    return None


def parse_option_symbol(symbol: str):
    import re
    if not isinstance(symbol, str) or not symbol:
        return None
    sym = symbol.replace(" ", "").strip().upper()
    if not (sym.endswith("CE") or sym.endswith("PE")):
        return None

    opt_type = sym[-2:]
    rem = sym[:-2]

    underlying = None
    for idx in ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTY"]:
        if rem.startswith(idx):
            underlying = idx
            break
    if not underlying:
        return None

    rem = rem[len(underlying):]

    month_map_3letter = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
    }

    month_map_char = {
        "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "O": 10, "N": 11, "D": 12
    }

    min_strikes = {
        "NIFTY": 500, "BANKNIFTY": 500, "FINNIFTY": 500,
        "MIDCPNIFTY": 500, "SENSEX": 500, "BANKEX": 500
    }
    min_strike = min_strikes.get(underlying, 100)
    current_year = datetime.now(IST).year

    # 1. AngelOne Weekly: {DD}{MMM}{YY}{Strike}
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(\d+)$", rem)
    if m:
        day = int(m.group(1))
        month_str = m.group(2)
        year = int(m.group(3)) + 2000
        strike = int(m.group(4))
        if (month_str in month_map_3letter and 1 <= day <= 31
                and strike >= min_strike and (current_year - 1 <= year <= current_year + 1)):
            return {
                "underlying": underlying, "year": year,
                "month": month_map_3letter[month_str], "day": day,
                "strike": strike, "opt_type": opt_type,
            }

    # 2. NSE Weekly: {YY}{M}{DD}{Strike}
    m = re.match(r"^(\d{2})([1-9OND])(\d{2})(\d+)$", rem)
    if m:
        year = int(m.group(1)) + 2000
        month_str = m.group(2)
        day = int(m.group(3))
        strike = int(m.group(4))
        if (month_str in month_map_char and 1 <= day <= 31
                and strike >= min_strike and (current_year - 1 <= year <= current_year + 1)):
            return {
                "underlying": underlying, "year": year,
                "month": month_map_char[month_str], "day": day,
                "strike": strike, "opt_type": opt_type,
            }

    # 3. Monthly: {YY}{MMM}{Strike}
    m = re.match(r"^(\d{2})([A-Z]{3})(\d+)$", rem)
    if m:
        year = int(m.group(1)) + 2000
        month_str = m.group(2)
        strike = int(m.group(3))
        if (month_str in month_map_3letter
                and strike >= min_strike and (current_year - 1 <= year <= current_year + 1)):
            return {
                "underlying": underlying, "year": year,
                "month": month_map_3letter[month_str], "day": 0,
                "strike": strike, "opt_type": opt_type,
            }

    return None


def get_last_thursday(year: int, month: int) -> int:
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    for d in range(last_day, 0, -1):
        if calendar.weekday(year, month, d) == 3:
            return d
    return 0


def are_symbols_equivalent(sym1: str, sym2: str) -> bool:
    if not isinstance(sym1, str) or not isinstance(sym2, str):
        return False
    s1 = sym1.replace(" ", "").strip().upper()
    s2 = sym2.replace(" ", "").strip().upper()
    if s1 == s2:
        return True
    p1 = parse_option_symbol(s1)
    p2 = parse_option_symbol(s2)
    if not p1 or not p2:
        return False
    if p1["underlying"] != p2["underlying"]:
        return False
    if p1["strike"] != p2["strike"]:
        return False
    if p1["opt_type"] != p2["opt_type"]:
        return False
    if p1["year"] != p2["year"]:
        return False
    if p1["month"] != p2["month"]:
        return False
    # If either is monthly format (day=0), skip day comparison
    if p1["day"] == 0 or p2["day"] == 0:
        return True
    return p1["day"] == p2["day"]


# ──────────────────────────────────────────────────────────
#  LIVE LTP  (via AngelOne)
# ──────────────────────────────────────────────────────────

async def get_live_ltp(client_id: str, exchange: str, trading_symbol: str) -> float:
    """
    Fetch live LTP from AngelOne using the instrument token
    from the loaded ScripMaster (instruments.py).
    """
    try:
        from .instruments import get_token
        from .angel_broker import get_ltp

        token = get_token(trading_symbol)
        if not token:
            logger.warning(f"[LTP] No token found for symbol: {trading_symbol}")
            return 0.0

        ltp = await get_ltp(client_id, token, exchange.upper())
        if ltp > 0:
            logger.info(f"[LTP] {trading_symbol} → ₹{ltp} (via AngelOne)")
            return ltp

        logger.warning(f"[LTP] AngelOne returned 0 for {trading_symbol} (token={token})")
        return 0.0

    except Exception as e:
        logger.error(f"[LTP] Exception fetching LTP for {trading_symbol}: {e}", exc_info=True)
        return 0.0


# ──────────────────────────────────────────────────────────
#  ORDER VERIFICATION  (via AngelOne)
# ──────────────────────────────────────────────────────────

async def verify_order_status(client_id: str, order_id: str, trading_symbol: str = "", max_retries: int = 3) -> dict:
    """Verify fill status by checking AngelOne order book."""
    from .angel_broker import verify_angelone_order
    return await verify_angelone_order(client_id, order_id, max_retries=max_retries)


# ──────────────────────────────────────────────────────────
#  BROKER POSITION CHECK  (via AngelOne)
# ──────────────────────────────────────────────────────────

async def check_broker_position(client_id: str, trading_symbol: str, expected_qty: int = 0) -> dict:
    """Check AngelOne positions API to see if a position still exists."""
    from .angel_broker import check_angelone_position
    return await check_angelone_position(client_id, trading_symbol, expected_qty)


# ──────────────────────────────────────────────────────────
#  PLACE ORDER  (via AngelOne)
# ──────────────────────────────────────────────────────────

async def place_angelone_order(client_id: str, exchange: str, token: str, side: str,
                               qty: int, price: float, product: str = "INTRADAY",
                               trading_symbol: str = "", order_type: str = "LIMIT") -> dict:
    """
    Thin wrapper kept for backward-compat with scheduler.py calls.
    Delegates to angel_broker.place_angelone_order.
    """
    from .angel_broker import place_angelone_order as _place
    return await _place(
        client_id=client_id,
        exchange=exchange,
        token=str(token),
        trading_symbol=trading_symbol,
        side=side,
        qty=qty,
        price=price,
        product=product,
        order_type=order_type,
    )


# ──────────────────────────────────────────────────────────
#  EXECUTE TRADE (called by scheduler & manual route)
# ──────────────────────────────────────────────────────────

async def execute_trade(direction: str, spot_price: float, config: dict):
    """
    Full trade execution pipeline:
      1. Check cooldown (no duplicate trades)
      2. Find exact option symbol + token (from AngelOne ScripMaster)
      3. Fetch live LTP via AngelOne (ABORT if zero)
      4. Place order with AngelOne
      5. Verify order in AngelOne order book
      6. Record in DB only if broker confirms fill
    """
    global _last_trade_info
    db = get_db()

    # ── Guard 1: Cooldown ──
    now = datetime.now(IST)
    if (_last_trade_info["direction"] == direction
            and _last_trade_info["timestamp"]
            and (now - _last_trade_info["timestamp"]).total_seconds() < TRADE_COOLDOWN_SECONDS):
        elapsed = (now - _last_trade_info["timestamp"]).total_seconds()
        logger.warning(
            f"[EXEC] ❌ GUARD-1 COOLDOWN: {direction} trade fired {elapsed:.0f}s ago "
            f"on {_last_trade_info['symbol']}. Skipping duplicate."
        )
        return None

    # ── Guard 2: No active trades allowed ──
    open_trade = await db.trades.find_one({"status": "OPEN"})
    if open_trade:
        logger.warning(
            f"[EXEC] ❌ GUARD-2 ACTIVE TRADE: Already have {open_trade['symbol']} "
            f"({open_trade['direction']}) OPEN. Skipping."
        )
        return None

    # ── Guard 3: Recent same-direction trade ──
    recent_cutoff = get_ist_time() - timedelta(seconds=TRADE_COOLDOWN_SECONDS)
    recent_same_dir = await db.trades.find_one({
        "direction": direction,
        "entry_time": {"$gte": recent_cutoff},
    })
    if recent_same_dir:
        logger.warning(
            f"[EXEC] ❌ GUARD-3 RECENT TRADE: {direction} trade on "
            f"{recent_same_dir['symbol']} placed recently. Cooldown active."
        )
        return None

    # ── Step 1: Strike math ──
    import math
    strike_selection = config.get("strike_selection", "A")
    option_type = "CE" if direction == "BUY" else "PE"
    if strike_selection == "B":
        if option_type == "CE":
            base_strike = math.floor(spot_price / 50.0) * 50
        else:
            base_strike = math.ceil(spot_price / 50.0) * 50
    else:
        base_strike = round(spot_price / 50.0) * 50
        if option_type == "CE":
            base_strike = base_strike + 50
        elif option_type == "PE":
            base_strike = base_strike - 100
    base_symbol_prefix = config.get("symbol", "NIFTY").upper()

    from .instruments import get_exact_option_symbol, get_token, get_lot_size

    exact_symbol = get_exact_option_symbol(base_symbol_prefix, base_strike, option_type)
    if not exact_symbol:
        logger.error(f"[EXEC] Could not find option contract for {base_symbol_prefix} {base_strike} {option_type}")
        return None

    # ── Step 2: Instrument token (from AngelOne ScripMaster) ──
    instrument_token = get_token(exact_symbol)
    if not instrument_token:
        logger.error(f"[EXEC] No instrument token found for {exact_symbol}")
        return None

    client_id = ANGEL_CLIENT_ID
    exchange_target = config.get("exchange", "NFO")

    # ── Step 3: Lot size ──
    lot_size = get_lot_size(exact_symbol)
    final_qty = int(config.get("quantity", 1)) * lot_size

    # ── Step 4: Live LTP — ABORT if zero ──
    option_ltp = await get_live_ltp(client_id, exchange_target, exact_symbol)
    if option_ltp <= 0:
        logger.error(
            f"[EXEC] ❌ ABORTING: LTP for {exact_symbol} = {option_ltp}. "
            f"Cannot place order with zero/invalid price!"
        )
        return None

    limit_buffer = float(config.get("limit_buffer", 0.0))
    limit_price = round(round((option_ltp + limit_buffer) / 0.05) * 0.05, 2)
    logger.info(
        f"[EXEC] {exact_symbol} | token={instrument_token} | LTP={option_ltp} | "
        f"buffer={limit_buffer} | limit_price={limit_price} | qty={final_qty}"
    )

    # ── Step 5: Place order ──
    order_res = await place_angelone_order(
        client_id=client_id,
        exchange=exchange_target,
        token=instrument_token,
        side="BUY",
        qty=final_qty,
        price=limit_price,
        product="INTRADAY",
        trading_symbol=exact_symbol,
    )

    if order_res.get("status") != "success":
        err_msg = order_res.get("message", "Unknown submission error")
        logger.error(f"[EXEC] ❌ Broker rejected: {err_msg}")
        await db.trade_errors.insert_one({
            "symbol": exact_symbol, "direction": direction,
            "attempted_price": limit_price, "ltp": option_ltp,
            "error": err_msg, "timestamp": get_ist_time(),
        })
        new_trade = {
            "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
            "direction": direction, "entry_price": limit_price,
            "quantity": final_qty, "status": "REJECTED",
            "entry_time": get_ist_time(), "order_id": None,
            "broker_verification": "FAILED",
            "close_reason": f"SUBMISSION FAILED: {err_msg}", "payload": order_res,
        }
        await db.trades.insert_one(new_trade)
        _last_trade_info.update({"direction": direction, "timestamp": now, "symbol": exact_symbol})
        return new_trade

    order_id = order_res.get("order_id")
    logger.info(f"[EXEC] Order submitted. order_id={order_id}")

    # ── Step 6: Verify fill ──
    if order_id:
        verification = await verify_order_status(client_id, order_id, trading_symbol=exact_symbol)
        logger.info(f"[VERIFY] {verification}")

        if verification["status"] == "REJECTED":
            logger.error(f"[EXEC] ❌ REJECTED by broker: {verification['reason']}")
            await db.trade_errors.insert_one({
                "symbol": exact_symbol, "direction": direction,
                "attempted_price": limit_price, "ltp": option_ltp,
                "order_id": order_id,
                "error": f"REJECTED: {verification['reason']}",
                "timestamp": get_ist_time(),
            })
            new_trade = {
                "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
                "direction": direction, "entry_price": limit_price,
                "quantity": final_qty, "status": "REJECTED",
                "entry_time": get_ist_time(), "order_id": order_id,
                "broker_verification": "REJECTED",
                "close_reason": f"REJECTED: {verification['reason']}", "payload": order_res,
            }
            await db.trades.insert_one(new_trade)
            _last_trade_info.update({"direction": direction, "timestamp": now, "symbol": exact_symbol})
            return new_trade

        if verification["status"] == "FILLED":
            actual_entry_price = verification["avg_price"] if verification["avg_price"] > 0 else limit_price
            actual_qty = verification["filled_qty"] if verification["filled_qty"] > 0 else final_qty
            logger.info(f"[EXEC] ✅ FILLED: {exact_symbol} @ ₹{actual_entry_price} × {actual_qty}")
            new_trade = {
                "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
                "direction": direction, "entry_price": actual_entry_price,
                "quantity": actual_qty, "status": "OPEN",
                "entry_time": get_ist_time(), "order_id": order_id,
                "broker_verification": "FILLED", "payload": order_res,
            }
            await db.trades.insert_one(new_trade)
            _last_trade_info.update({"direction": direction, "timestamp": now, "symbol": exact_symbol})
            return new_trade

        # Not FILLED (pending/unknown) — record as REJECTED
        logger.warning(f"[EXEC] ❌ Not filled: {verification['status']} — {verification['reason']}")
        new_trade = {
            "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
            "direction": direction, "entry_price": limit_price,
            "quantity": final_qty, "status": "REJECTED",
            "entry_time": get_ist_time(), "order_id": order_id,
            "broker_verification": verification.get("status", "UNVERIFIED"),
            "close_reason": f"NOT FILLED: {verification['status']} - {verification['reason']}",
            "payload": order_res,
        }
        await db.trades.insert_one(new_trade)
        _last_trade_info.update({"direction": direction, "timestamp": now, "symbol": exact_symbol})
        return new_trade

    # No order_id
    logger.warning("[EXEC] ❌ No order_id returned by broker.")
    new_trade = {
        "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
        "direction": direction, "entry_price": limit_price,
        "quantity": final_qty, "status": "REJECTED",
        "entry_time": get_ist_time(), "order_id": None,
        "broker_verification": "UNVERIFIED",
        "close_reason": "No order_id returned by broker", "payload": order_res,
    }
    await db.trades.insert_one(new_trade)
    _last_trade_info.update({"direction": direction, "timestamp": now, "symbol": exact_symbol})
    return new_trade
