"""
AngelOne SmartAPI Authentication Module.

Replaces the MasterTrust Selenium OAuth flow.
Performs TOTP-based login and stores the JWT in MongoDB (no Redis).
"""
import os
import logging
import asyncio
import pyotp
import requests as _requests
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("vma.angel_auth")

IST = timezone(timedelta(hours=5, minutes=30))
ANGEL_BASE_URL = "https://apiconnect.angelone.in"


def _no_auth_headers() -> dict:
    """Headers for unauthenticated AngelOne requests (login)."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": os.getenv("ANGEL_API_KEY", ""),
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
    }


def generate_totp(secret: str) -> str:
    """Generate the current 6-digit TOTP from a base32 secret."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def run_auto_login() -> dict:
    """
    Performs TOTP-based AngelOne SmartAPI login (synchronous).
    Reads credentials from env, generates TOTP, calls loginByPassword,
    and stores the JWT token in MongoDB sessions collection.

    Safe to call from a thread pool (asyncio.to_thread).
    Returns: {success: bool, message: str}
    """
    client_id = os.getenv("ANGEL_CLIENT_ID", "")
    mpin = os.getenv("ANGEL_MPIN", "")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "")
    api_key = os.getenv("ANGEL_API_KEY", "")

    if not all([client_id, mpin, totp_secret, api_key]):
        msg = "Missing AngelOne credentials in .env (need ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET, ANGEL_API_KEY)"
        logger.error(f"[ANGEL-LOGIN] {msg}")
        return {"success": False, "message": msg}

    totp_code = generate_totp(totp_secret)
    logger.info(f"[ANGEL-LOGIN] Generated TOTP for {client_id}: {totp_code}")

    payload = {
        "clientcode": client_id,
        "password": mpin,
        "totp": totp_code,
    }
    url = f"{ANGEL_BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"

    try:
        resp = _requests.post(url, json=payload, headers=_no_auth_headers(), timeout=15)
        data = resp.json()
        logger.info(f"[ANGEL-LOGIN] Response status={resp.status_code}: {data}")

        if resp.status_code == 200 and data.get("status"):
            token_data = data.get("data", {})
            jwt_token = token_data.get("jwtToken", "")
            refresh_token = token_data.get("refreshToken", "")
            feed_token = token_data.get("feedToken", "")

            if not jwt_token:
                return {"success": False, "message": "Login succeeded but jwtToken was empty in response"}

            # Persist token to MongoDB
            from ..database import get_sync_db
            db = get_sync_db()
            now = datetime.now(IST).replace(tzinfo=None)
            
            # Safely drop legacy token_1 index and clean corrupt null token docs
            try:
                db.sessions.drop_index("token_1")
            except Exception:
                pass
            try:
                db.sessions.delete_many({"token": None})
            except Exception:
                pass

            db.sessions.update_one(
                {"client_id": client_id},
                {"$set": {
                    "client_id": client_id,
                    "token": jwt_token,
                    "jwt_token": jwt_token,
                    "refresh_token": refresh_token,
                    "feed_token": feed_token,
                    "broker": "angelone",
                    "generated_at": now,
                }},
                upsert=True,
            )
            logger.info(f"[ANGEL-LOGIN] ✅ Token saved to MongoDB for {client_id}")
            return {
                "success": True,
                "message": f"AngelOne login successful for {client_id}",
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S IST"),
            }

        # Login failed
        msg = data.get("message", "Unknown login error")
        logger.error(f"[ANGEL-LOGIN] ❌ Login failed: {msg}")
        return {"success": False, "message": msg}

    except Exception as e:
        logger.error(f"[ANGEL-LOGIN] Exception during login: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


def get_angel_token(client_id: str) -> str:
    """
    Retrieve the current AngelOne JWT from MongoDB sessions.
    Returns empty string if no session found.
    """
    try:
        from ..database import get_sync_db
        db = get_sync_db()
        session = db.sessions.find_one(
            {"client_id": client_id, "broker": "angelone"},
            {"jwt_token": 1, "_id": 0},
        )
        if session:
            return session.get("jwt_token", "")
    except Exception as e:
        logger.error(f"[ANGEL-AUTH] Error reading token from MongoDB: {e}")
    return ""


def get_session_info(client_id: str) -> dict:
    """
    Returns full session info for a client (for the /api/auth/user endpoint).
    """
    try:
        from ..database import get_sync_db
        db = get_sync_db()
        session = db.sessions.find_one(
            {"client_id": client_id, "broker": "angelone"},
            {"jwt_token": 0, "refresh_token": 0, "feed_token": 0, "_id": 0},
        )
        if session:
            gen_at = session.get("generated_at")
            return {
                "logged_in": True,
                "client_id": client_id,
                "broker": "angelone",
                "last_login": gen_at.strftime("%Y-%m-%d %H:%M:%S IST") if gen_at else "Unknown",
            }
    except Exception as e:
        logger.error(f"[ANGEL-AUTH] Error reading session info: {e}")
    return {"logged_in": False, "client_id": client_id}
