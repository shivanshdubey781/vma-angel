from fastapi import APIRouter
import asyncio

from ..config import ANGEL_CLIENT_ID
from .angel_auth import run_auto_login, get_session_info

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login():
    """
    Trigger AngelOne TOTP-based login.
    Generates TOTP from stored secret, calls SmartAPI, saves JWT to MongoDB.
    """
    if not ANGEL_CLIENT_ID:
        return {"success": False, "message": "ANGEL_CLIENT_ID not configured in .env"}

    result = await asyncio.to_thread(run_auto_login)
    return result


@router.get("/user")
async def get_user():
    """
    Return the current session status from MongoDB.
    """
    if not ANGEL_CLIENT_ID:
        return {"logged_in": False}

    info = await asyncio.to_thread(get_session_info, ANGEL_CLIENT_ID)
    return info
