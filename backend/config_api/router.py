from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from ..database import get_db

router = APIRouter(prefix="/api/config", tags=["config"])

class ConfigUpdate(BaseModel):
    fast_period: int
    slow_period: int
    target_pts: int
    sl_pts: int
    trail_trigger: int
    trail_pts: int
    quantity: int
    limit_buffer: float
    symbol: str
    exchange: str
    interval: str
    immediate_entry: Optional[bool] = False
    strike_selection: Optional[str] = "A"

async def get_current_config(db=None):
    if db is None:
        from ..database import get_db
        db = get_db()
    conf = await db.config.find_one({"_id": "vma_config"})
    return conf or {}

@router.get("")
async def get_config():
    conf = await get_current_config()
    if conf:
        conf.pop("_id", None)
        return conf
    return {}

@router.post("")
async def update_config(config: ConfigUpdate):
    db = get_db()
    data = config.dict()
    await db.config.update_one({"_id": "vma_config"}, {"$set": data}, upsert=True)
    return {"success": True, "message": "Configuration saved"}
