from fastapi import APIRouter
from ..database import get_db

router = APIRouter(prefix="/api/history", tags=["history"])

@router.get("")
async def get_history():
    db = get_db()
    # Filter out ghost trades (entry_price <= 1.0 or close_reason starts with INVALID)
    query = {
        "entry_price": {"$gte": 1.0},
        "close_reason": {"$not": {"$regex": "^INVALID:"}}
    }
    trades = await db.trades.find(query).sort("entry_time", -1).limit(100).to_list(length=100)
    
    formatted = []
    for t in trades:
        def to_ist(dt):
            if not dt: return None
            # Since new timestamps are stored directly in IST, we format it directly
            return dt.strftime("%Y-%m-%d %I:%M:%S %p")
            
        formatted.append({
            "trade_id": t.get("trade_id", str(t.get("_id"))),
            "symbol": t.get("symbol", ""),
            "direction": t.get("direction", ""),
            "status": t.get("status", ""),
            "entry_time": to_ist(t.get("entry_time")),
            "exit_time": to_ist(t.get("exit_time")),
            "entry_price": t.get("entry_price"),
            "exit_price": t.get("exit_price"),
            "quantity": t.get("quantity", 1),
            "close_reason": t.get("close_reason", "")
        })
        
    return {"success": True, "trades": formatted}
