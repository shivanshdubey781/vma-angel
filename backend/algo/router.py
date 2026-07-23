from fastapi import APIRouter, Depends
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_time():
    return datetime.now(IST).replace(tzinfo=None)

from ..database import get_db
from ..config_api.router import get_current_config
from .ohlc_source import fetch_latest_close

router = APIRouter(prefix="/api/algo", tags=["algo"])

class ToggleRequest(BaseModel):
    enabled: bool

@router.post("/toggle")
async def toggle_algo(req: ToggleRequest):
    db = get_db()
    await db.config.update_one(
        {"_id": "vma_config"},
        {"$set": {"algo_enabled": req.enabled, "updated_at": get_ist_time()}},
        upsert=True,
    )
    return {"success": True, "algo_enabled": req.enabled}

@router.get("/status")
async def algo_status():
    db = get_db()
    doc = await get_current_config(db)

    last_signal_doc = await db.signals.find_one({}, sort=[("timestamp", -1)])
    last_trade_doc = await db.trades.find_one({}, sort=[("entry_time", -1)])
    open_trades = await db.trades.find({"status": "OPEN"}).to_list(length=100)

    from .executor import get_live_ltp
    from ..config import ANGEL_CLIENT_ID
    client_id = ANGEL_CLIENT_ID
    for t in open_trades:
        live_ltp = await get_live_ltp(client_id, "NFO", t["symbol"])
        if live_ltp > 0:
            t["current_ltp"] = live_ltp
            await db.trades.update_one({"_id": t["_id"]}, {"$set": {"current_ltp": live_ltp}})

    # serialize ObjectId and dates
    for t in open_trades:
        t["_id"] = str(t["_id"])
        if "entry_time" in t and t["entry_time"]:
            et = t["entry_time"]
            if et.tzinfo is None:
                et = et.replace(tzinfo=IST)
            t["entry_time"] = et.isoformat()
        if "exit_time" in t and t["exit_time"]:
            ext = t["exit_time"]
            if ext.tzinfo is None:
                ext = ext.replace(tzinfo=IST)
            t["exit_time"] = ext.isoformat()

    last_trade_at = None
    if last_trade_doc and last_trade_doc.get("entry_time"):
        lt = last_trade_doc.get("entry_time")
        if lt.tzinfo is None:
            lt = lt.replace(tzinfo=IST)
        last_trade_at = lt.isoformat()

    return {
        "algo_enabled": doc.get("algo_enabled", False),
        "last_signal": last_signal_doc.get("signal") if last_signal_doc else "NONE",
        "last_trade_at": last_trade_at,
        "open_trades": open_trades,
        "fast_vma": last_signal_doc.get("fast_vma", 0.0) if last_signal_doc else 0.0,
        "slow_vma": last_signal_doc.get("slow_vma", 0.0) if last_signal_doc else 0.0,
    }

@router.get("/nearest_strike")
async def get_nearest_strike():
    db = get_db()
    config = await get_current_config(db)
    interval = config.get("interval", "ONE_MINUTE")
    spot_price = await fetch_latest_close(interval) or 0.0

    if spot_price <= 0:
        import httpx
        try:
            url = "https://www.nseindia.com/api/marketStatus"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient() as client:
                res = await client.get(url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    for m in data.get("marketState", []):
                        if m.get("index") == "NIFTY 50":
                            spot_price = float(m.get("last", 0))
                            break
        except Exception:
            pass

    if spot_price <= 0:
        spot_price = 23300.0  # last-resort fallback

    import math
    strike_selection = config.get("strike_selection", "A")
    if strike_selection == "B":
        ce_strike = math.floor(spot_price / 50.0) * 50
        pe_strike = math.ceil(spot_price / 50.0) * 50
        base_strike = ce_strike
    else:
        base_strike = round(spot_price / 50.0) * 50
        ce_strike = base_strike + 50
        pe_strike = base_strike - 100

    from .instruments import get_exact_option_symbol
    from .executor import get_live_ltp
    from ..config import ANGEL_CLIENT_ID

    ce_symbol = get_exact_option_symbol("NIFTY", ce_strike, "CE")
    pe_symbol = get_exact_option_symbol("NIFTY", pe_strike, "PE")

    client_id = ANGEL_CLIENT_ID
    ce_ltp = await get_live_ltp(client_id, "NFO", ce_symbol) if ce_symbol else 0.0
    pe_ltp = await get_live_ltp(client_id, "NFO", pe_symbol) if pe_symbol else 0.0

    return {
        "success": True,
        "spot_price": spot_price,
        "nearest_strike": base_strike,
        "ce_symbol": ce_symbol or f"NIFTY {ce_strike} CE",
        "ce_price": ce_ltp,
        "pe_symbol": pe_symbol or f"NIFTY {pe_strike} PE",
        "pe_price": pe_ltp,
    }

class ManualTradeRequest(BaseModel):
    direction: str
    quantity: int
    buffer: float

@router.post("/manual_trade")
async def manual_trade(req: ManualTradeRequest):
    from .executor import place_angelone_order, get_live_ltp
    from .instruments import get_exact_option_symbol, get_token, get_lot_size
    from ..config import ANGEL_CLIENT_ID

    db = get_db()
    config = await get_current_config(db)

    interval = config.get("interval", "ONE_MINUTE")
    spot_price = await fetch_latest_close(interval) or 0.0

    if spot_price <= 0:
        import httpx
        try:
            url = "https://www.nseindia.com/api/marketStatus"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient() as client:
                res = await client.get(url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    for m in data.get("marketState", []):
                        if m.get("index") == "NIFTY 50":
                            spot_price = float(m.get("last", 0))
                            break
        except Exception:
            pass

    if spot_price <= 0:
        return {"success": False, "message": "Failed to fetch Nifty spot price for ATM calculation."}

    import math
    strike_selection = config.get("strike_selection", "A")
    option_type = "CE" if req.direction.upper() == "BUY" else "PE"
    if strike_selection == "B":
        base_strike = math.floor(spot_price / 50.0) * 50 if option_type == "CE" else math.ceil(spot_price / 50.0) * 50
    else:
        base_strike = round(spot_price / 50.0) * 50
        if option_type == "CE":
            base_strike = base_strike + 50
        elif option_type == "PE":
            base_strike = base_strike - 100

    exact_symbol = get_exact_option_symbol("NIFTY", base_strike, option_type)
    if not exact_symbol:
        return {"success": False, "message": f"Could not find option contract for NIFTY {base_strike} {option_type}"}

    client_id = ANGEL_CLIENT_ID
    token = get_token(exact_symbol)
    if not token:
        return {"success": False, "message": f"No instrument token found for {exact_symbol}"}

    lot_size = get_lot_size(exact_symbol)
    final_qty = req.quantity * lot_size

    option_ltp = await get_live_ltp(client_id, "NFO", exact_symbol)
    if option_ltp <= 0.0:
        return {"success": False, "message": f"Could not fetch LTP for {exact_symbol} from AngelOne."}

    limit_price = round(round((option_ltp + req.buffer) / 0.05) * 0.05, 2)

    res = await place_angelone_order(
        client_id=client_id,
        exchange="NFO",
        token=token,
        side="BUY",
        qty=final_qty,
        price=limit_price,
        product="INTRADAY",
        trading_symbol=exact_symbol,
    )

    import uuid
    if res.get("status") != "success":
        err_msg = res.get("message", "Unknown submission error")
        await db.trades.insert_one({
            "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
            "direction": req.direction.upper(), "entry_price": limit_price,
            "quantity": final_qty, "status": "REJECTED",
            "entry_time": get_ist_time(), "order_id": None,
            "broker_verification": "FAILED",
            "close_reason": f"SUBMISSION FAILED: {err_msg}", "payload": res,
        })
        return {"success": False, "message": f"Broker Error: {err_msg}"}

    order_id = res.get("order_id")
    actual_entry_price = limit_price
    broker_status = "UNVERIFIED"
    verification = {}

    if order_id:
        from .executor import verify_order_status
        verification = await verify_order_status(client_id, order_id, trading_symbol=exact_symbol)
        broker_status = verification.get("status", "UNKNOWN")

        if broker_status == "REJECTED":
            rej_reason = verification.get("reason", "Unknown reason")
            await db.trades.insert_one({
                "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
                "direction": req.direction.upper(), "entry_price": limit_price,
                "quantity": final_qty, "status": "REJECTED",
                "entry_time": get_ist_time(), "order_id": order_id,
                "broker_verification": "REJECTED",
                "close_reason": f"REJECTED: {rej_reason}", "payload": res,
            })
            return {"success": False, "message": f"❌ Order REJECTED by broker: {rej_reason}"}

    if broker_status == "FILLED":
        if verification.get("avg_price", 0) > 0:
            actual_entry_price = verification["avg_price"]
        await db.trades.insert_one({
            "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
            "direction": req.direction.upper(), "entry_price": actual_entry_price,
            "quantity": final_qty, "status": "OPEN",
            "entry_time": get_ist_time(), "order_id": order_id,
            "broker_verification": broker_status, "payload": res,
        })
        return {"success": True, "message": f"Trade Placed: {exact_symbol} at ₹{actual_entry_price} (Broker: {broker_status})", "data": res}
    else:
        await db.trades.insert_one({
            "trade_id": str(uuid.uuid4()), "symbol": exact_symbol,
            "direction": req.direction.upper(), "entry_price": limit_price,
            "quantity": final_qty, "status": "REJECTED",
            "entry_time": get_ist_time(), "order_id": order_id,
            "broker_verification": broker_status,
            "close_reason": f"NOT FILLED: {broker_status} - {verification.get('reason', 'Unknown')}" if order_id else "No order_id",
            "payload": res,
        })
        return {"success": False, "message": f"❌ Order not filled: {broker_status}"}

class FakeSignalRequest(BaseModel):
    signal: str

@router.post("/fake_signal")
async def fake_signal(req: FakeSignalRequest):
    if req.signal not in ["CE", "PE"]:
        return {"success": False, "message": "Invalid signal. Must be CE or PE."}
    db = get_db()
    config = await db.config.find_one({"_id": "vma_config"})
    if not config or not config.get("algo_enabled", False):
        return {"success": False, "message": "VMA Algo is disabled. Please enable it first."}
    await db.config.update_one({"_id": "vma_config"}, {"$set": {"force_signal": req.signal}})
    return {"success": True, "message": f"Fake VMA {req.signal} entry triggered! Runs on next tick."}

class ManualExitRequest(BaseModel):
    trade_id: str

@router.post("/manual_exit")
async def manual_exit(req: ManualExitRequest):
    db = get_db()
    trade = await db.trades.find_one({"trade_id": req.trade_id, "status": "OPEN"})
    if not trade:
        from bson import ObjectId
        try:
            trade = await db.trades.find_one({"_id": ObjectId(req.trade_id), "status": "OPEN"})
        except Exception:
            pass

    if not trade:
        return {"success": False, "message": "Trade not found or already closed."}

    from .executor import place_angelone_order, get_live_ltp, check_broker_position
    from .instruments import get_token
    from ..config import ANGEL_CLIENT_ID

    symbol = trade["symbol"]
    qty = trade["quantity"]
    client_id = ANGEL_CLIENT_ID
    token = get_token(symbol)
    if not token:
        return {"success": False, "message": "Instrument token not found for exit."}

    ltp = await get_live_ltp(client_id, "NFO", symbol)
    if ltp <= 0:
        ltp = 150.0  # safe fallback

    exit_price_rounded = round(round(ltp / 0.05) * 0.05, 2)

    order_id = trade.get("order_id", "")
    is_real_trade = (
        order_id
        and not str(order_id).startswith("FAKE_ORDER_")
        and trade.get("broker_verification") == "FILLED"
    )

    if is_real_trade:
        pos_result = await check_broker_position(client_id, symbol, qty)
        if not pos_result["has_position"]:
            await db.trades.update_one(
                {"_id": trade["_id"]},
                {"$set": {"status": "CLOSED", "exit_time": get_ist_time(), "exit_price": ltp,
                           "close_reason": "MANUAL EXIT (already closed on broker)"}},
            )
            return {"success": True, "message": f"{symbol} closed in dashboard (position already exited on broker)."}

        # Use aggressive exit price (5% below LTP to guarantee fill)
        aggressive_price = round(round((ltp * 0.95) / 0.05) * 0.05, 2)
        if aggressive_price <= 0:
            aggressive_price = 0.05

        res = await place_angelone_order(
            client_id=client_id, exchange="NFO", token=token,
            side="SELL", qty=qty, price=aggressive_price,
            product="INTRADAY", trading_symbol=symbol,
        )
        if res.get("status") != "success":
            return {"success": False, "message": f"Broker Error: {res.get('message')}"}
    else:
        res = {"status": "success", "message": "Simulated manual exit"}

    await db.trades.update_one(
        {"_id": trade["_id"]},
        {"$set": {"status": "CLOSED", "exit_time": get_ist_time(),
                  "exit_price": ltp, "close_reason": "MANUAL EXIT"}},
    )
    return {"success": True, "message": f"Successfully exited {symbol} at ₹{ltp}"}

@router.get("/search_symbols")
async def api_search_symbols(q: str = ""):
    return {"success": True, "symbols": []}

@router.get("/ltp")
async def get_ltp(symbol: str):
    from .executor import get_live_ltp
    from ..config import ANGEL_CLIENT_ID
    try:
        ltp = await get_live_ltp(ANGEL_CLIENT_ID, "NFO", symbol)
        return {"success": True, "ltp": ltp}
    except Exception as e:
        return {"success": False, "ltp": 0.0, "error": str(e)}

@router.get("/signals_history")
async def get_signals_history(limit: int = 30):
    """Fetch recent VMA signals for the signals table."""
    db = get_db()
    try:
        signals = await db.signals.find({}).sort("timestamp", -1).limit(limit).to_list(length=limit)
        for sig in signals:
            if "_id" in sig:
                sig["_id"] = str(sig["_id"])
            if "timestamp" in sig and sig["timestamp"]:
                ts = sig["timestamp"]
                if hasattr(ts, "isoformat"):
                    sig["timestamp"] = ts.isoformat()
        return {"success": True, "signals": signals}
    except Exception as e:
        return {"success": False, "error": str(e), "signals": []}
