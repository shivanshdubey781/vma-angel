"""
AngelOne SmartAPI broker integration.

Replaces MasterTrust (mt_option_chain.py + executor.py broker calls).
Provides: token lookup, LTP fetching, order placement, order verification,
position checking — all via AngelOne REST API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("vma.angel_broker")

ANGEL_BASE_URL = "https://apiconnect.angelone.in"


# ──────────────────────────────────────────────────────────
#  SHARED HELPERS
# ──────────────────────────────────────────────────────────

def _auth_headers(jwt_token: str) -> dict:
    """Build authenticated headers for AngelOne API calls."""
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": os.getenv("ANGEL_API_KEY", ""),
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
    }


def get_angel_token(client_id: str) -> str:
    """Read the live AngelOne JWT from MongoDB sessions."""
    try:
        from ..auth.angel_auth import get_angel_token as _get
        return _get(client_id)
    except Exception as e:
        logger.error(f"[ANGEL-BROKER] Could not read token: {e}")
        return ""


# ──────────────────────────────────────────────────────────
#  LIVE LTP
# ──────────────────────────────────────────────────────────

async def get_ltp(client_id: str, symbol_token: str, exchange: str = "NFO") -> float:
    """
    Fetch live LTP for a single instrument via AngelOne quote API.
    Uses the numeric symbol token (from AngelOne ScripMaster / instruments.py).

    API: POST /rest/secure/angelbroking/market/v1/quote/
    Body: {mode: "LTP", exchangeTokens: {<exchange>: [<token>]}}
    Returns 0.0 on any failure.
    """
    jwt = get_angel_token(client_id)
    if not jwt:
        logger.error(f"[ANGEL-LTP] No JWT token found for {client_id}. Please login first.")
        return 0.0

    url = f"{ANGEL_BASE_URL}/rest/secure/angelbroking/market/v1/quote/"
    payload = {
        "mode": "LTP",
        "exchangeTokens": {exchange: [str(symbol_token)]},
    }

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(url, json=payload, headers=_auth_headers(jwt))
            if res.status_code == 200:
                data = res.json()
                if data.get("status"):
                    fetched = data.get("data", {}).get("fetched", [])
                    if fetched:
                        ltp = float(fetched[0].get("ltp", 0) or 0)
                        logger.info(f"[ANGEL-LTP] token={symbol_token} exchange={exchange} → ltp={ltp}")
                        return ltp
                    logger.warning(f"[ANGEL-LTP] Empty fetched list for token={symbol_token}")
                else:
                    logger.warning(f"[ANGEL-LTP] API returned status=False: {data.get('message')}")
            else:
                logger.warning(f"[ANGEL-LTP] HTTP {res.status_code}: {res.text[:200]}")
    except Exception as e:
        logger.error(f"[ANGEL-LTP] Exception for token={symbol_token}: {e}", exc_info=True)

    return 0.0


# ──────────────────────────────────────────────────────────
#  ORDER PLACEMENT
# ──────────────────────────────────────────────────────────

async def place_angelone_order(
    client_id: str,
    exchange: str,
    token: str,
    trading_symbol: str,
    side: str,
    qty: int,
    price: float,
    product: str = "INTRADAY",
    order_type: str = "LIMIT",
) -> dict:
    """
    Place a BUY or SELL order via AngelOne SmartAPI.

    API: POST /rest/secure/angelbroking/order/v1/placeOrder
    Returns: {status: "success"|"error", order_id: str|None, message: str}
    """
    jwt = get_angel_token(client_id)
    if not jwt:
        return {
            "status": "error",
            "message": "No AngelOne token in DB. Please login first.",
            "order_id": None,
        }

    payload = {
        "variety": "NORMAL",
        "tradingsymbol": trading_symbol,
        "symboltoken": str(token),
        "transactiontype": side.upper(),        # BUY or SELL
        "exchange": exchange.upper(),
        "ordertype": order_type.upper(),        # LIMIT or MARKET
        "producttype": product.upper(),         # INTRADAY or DELIVERY
        "duration": "DAY",
        "price": round(price, 2) if order_type.upper() == "LIMIT" else 0,
        "squareoff": 0,
        "stoploss": 0,
        "quantity": qty,
    }

    url = f"{ANGEL_BASE_URL}/rest/secure/angelbroking/order/v1/placeOrder"
    logger.info(f"[ANGEL-ORDER] Submitting: {payload}")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(url, json=payload, headers=_auth_headers(jwt))
            logger.info(f"[ANGEL-ORDER] Response [{res.status_code}]: {res.text}")

            if res.status_code == 200:
                data = res.json()
                if data.get("status"):
                    order_id = data.get("data", {})
                    # AngelOne returns orderid as a string directly or nested
                    if isinstance(order_id, dict):
                        order_id = order_id.get("orderid", "")
                    elif isinstance(order_id, str):
                        order_id = order_id
                    else:
                        order_id = str(order_id)
                    return {"status": "success", "data": data, "order_id": str(order_id)}
                else:
                    return {
                        "status": "error",
                        "message": data.get("message", "Broker returned error"),
                        "order_id": None,
                    }
            else:
                return {"status": "error", "message": res.text, "order_id": None}

    except Exception as e:
        logger.error(f"[ANGEL-ORDER] Exception: {e}", exc_info=True)
        return {"status": "error", "message": str(e), "order_id": None}


# ──────────────────────────────────────────────────────────
#  ORDER VERIFICATION
# ──────────────────────────────────────────────────────────

async def verify_angelone_order(
    client_id: str,
    order_id: str,
    max_retries: int = 3,
) -> dict:
    """
    Verify order fill status via AngelOne order book.

    API: GET /rest/secure/angelbroking/order/v1/getOrderBook
    Returns: {verified, status: FILLED|REJECTED|PENDING|UNKNOWN, reason, filled_qty, avg_price}
    """
    jwt = get_angel_token(client_id)
    if not jwt:
        return {
            "verified": False, "status": "UNKNOWN",
            "reason": "No AngelOne token", "filled_qty": 0, "avg_price": 0.0,
        }

    url = f"{ANGEL_BASE_URL}/rest/secure/angelbroking/order/v1/getOrderBook"

    for attempt in range(max_retries):
        try:
            await asyncio.sleep(2)
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.get(url, headers=_auth_headers(jwt))
                if res.status_code != 200:
                    logger.warning(f"[ANGEL-VERIFY] HTTP {res.status_code} (attempt {attempt + 1})")
                    continue

                data = res.json()
                orders = data.get("data", []) or []
                if not isinstance(orders, list):
                    continue

                found = None
                for order in orders:
                    if str(order.get("orderid", "")) == str(order_id):
                        found = order
                        break

                if not found:
                    logger.warning(f"[ANGEL-VERIFY] Order {order_id} not found (attempt {attempt + 1}/{max_retries})")
                    continue

                status_raw = (found.get("orderstatus", "") or "").strip().lower()
                filled_qty = int(found.get("filledshares", 0) or 0)
                avg_price = float(found.get("averageprice", 0) or 0)
                text = found.get("text", "") or ""

                logger.info(
                    f"[ANGEL-VERIFY] {order_id}: status={status_raw}, "
                    f"filled={filled_qty}, avg_price={avg_price}"
                )

                if status_raw in ["complete", "executed", "filled", "traded"]:
                    return {
                        "verified": True, "status": "FILLED",
                        "reason": f"Filled at ₹{avg_price:.2f}",
                        "filled_qty": filled_qty, "avg_price": avg_price,
                    }
                elif status_raw in ["rejected", "cancelled", "canceled"]:
                    return {
                        "verified": True, "status": "REJECTED",
                        "reason": text or f"Order {status_raw} by broker",
                        "filled_qty": 0, "avg_price": 0.0,
                    }
                elif status_raw in ["open", "pending", "trigger pending",
                                    "put order req received", "modified", "after market order req received"]:
                    if attempt < max_retries - 1:
                        continue
                    return {
                        "verified": True, "status": "PENDING",
                        "reason": f"Still {status_raw} after {max_retries} checks",
                        "filled_qty": filled_qty, "avg_price": avg_price,
                    }
                else:
                    return {
                        "verified": True, "status": status_raw.upper(),
                        "reason": f"Status: {status_raw}",
                        "filled_qty": filled_qty, "avg_price": avg_price,
                    }

        except Exception as e:
            logger.error(f"[ANGEL-VERIFY] Error (attempt {attempt + 1}): {e}")

    return {
        "verified": False, "status": "UNKNOWN",
        "reason": f"Could not verify order {order_id} after {max_retries} retries",
        "filled_qty": 0, "avg_price": 0.0,
    }


# ──────────────────────────────────────────────────────────
#  POSITION CHECK
# ──────────────────────────────────────────────────────────

async def check_angelone_position(
    client_id: str,
    trading_symbol: str,
    expected_qty: int = 0,
) -> dict:
    """
    Check AngelOne positions to see if a symbol still has an open net position.

    API: GET /rest/secure/angelbroking/order/v1/getPosition
    Returns: {has_position: bool, net_qty: int, details: str}
    """
    jwt = get_angel_token(client_id)
    if not jwt:
        return {
            "has_position": True,
            "net_qty": expected_qty,
            "details": "No token — assuming position exists to be safe",
        }

    url = f"{ANGEL_BASE_URL}/rest/secure/angelbroking/order/v1/getPosition"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=_auth_headers(jwt))
            if res.status_code != 200:
                return {
                    "has_position": True, "net_qty": expected_qty,
                    "details": f"Positions API returned HTTP {res.status_code}",
                }

            data = res.json()
            if not data.get("status"):
                return {
                    "has_position": True, "net_qty": expected_qty,
                    "details": data.get("message", "Positions API error"),
                }

            positions = data.get("data", []) or []
            if not isinstance(positions, list):
                return {
                    "has_position": True, "net_qty": expected_qty,
                    "details": "Unexpected positions API response format",
                }

            # Use symbol equivalence to handle different naming formats
            from .executor import are_symbols_equivalent
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                sym = (
                    pos.get("tradingsymbol")
                    or pos.get("symbolname")
                    or pos.get("symbol")
                    or ""
                )
                if sym and are_symbols_equivalent(sym, trading_symbol):
                    try:
                        net_qty = int(pos.get("netqty", 0) or 0)
                    except Exception:
                        net_qty = 0

                    if net_qty != 0:
                        return {
                            "has_position": True, "net_qty": net_qty,
                            "details": f"Position found: netqty={net_qty}",
                        }
                    else:
                        return {
                            "has_position": False, "net_qty": 0,
                            "details": "Position found but netqty=0 (already closed)",
                        }

            return {
                "has_position": False, "net_qty": 0,
                "details": "Symbol not found in AngelOne positions",
            }

    except Exception as e:
        logger.error(f"[ANGEL-POS] Error checking position for {trading_symbol}: {e}", exc_info=True)
        return {
            "has_position": True, "net_qty": expected_qty,
            "details": f"Error: {str(e)}",
        }
