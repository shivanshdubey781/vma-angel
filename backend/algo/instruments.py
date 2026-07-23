import httpx
import logging

logger = logging.getLogger("vma.instruments")

TOKEN_MAP = {}
SYMBOL_LIST = []
LOT_SIZES = {}

async def load_instruments():
    global TOKEN_MAP, SYMBOL_LIST, LOT_SIZES
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    
    try:
        logger.info("Downloading reliable ScripMaster database...")
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30.0)
            data = resp.json()
            
            for item in data:
                exch = item.get("exch_seg")
                if exch in ["NSE", "NFO"]:
                    sym = item.get("symbol")
                    token = item.get("token")
                    
                    # Safely parse the lot size string to integer
                    try:
                        lot_size = int(item.get("lotsize", 1))
                    except:
                        lot_size = 1
                    
                    if sym and token:
                        TOKEN_MAP[sym] = str(token)
                        SYMBOL_LIST.append(sym)
                        LOT_SIZES[sym] = lot_size
                        
        logger.info(f"Successfully processed {len(TOKEN_MAP)} instruments.")
    except Exception as e:
        logger.error(f"Failed to load ScripMaster: {e}")

def get_token(symbol: str) -> str:
    return TOKEN_MAP.get(symbol.strip())
    
def get_lot_size(symbol: str) -> int:
    return LOT_SIZES.get(symbol.strip(), 1)

def search_symbols(query: str, limit: int = 100) -> list[str]:
    query = query.upper()
    parts = query.split()
    if not parts:
        return []
        
    results = []
    for sym in SYMBOL_LIST:
        if all(part in sym for part in parts):
            results.append(sym)
            if len(results) >= limit:
                break
    return results

def get_exact_option_symbol(base_name: str, strike: int, option_type: str) -> str:
    """
    Safely finds the exact option symbol for a specific strike and type,
    ensuring it matches the exact base_name and parses/sorts them by nearest expiry date.
    """
    import datetime
    base_name = base_name.upper()
    option_type = option_type.upper()
    strike_str = str(int(strike))
    
    matches = []
    for sym in SYMBOL_LIST:
        # 1. Must exactly start with the base prefix (e.g., NIFTY but NOT BANKNIFTY)
        if not sym.startswith(base_name):
            continue
            
        # 2. Prevent "NIFTY" from matching "NIFTYIT" etc., ensure next char is a digit
        if base_name == "NIFTY" and len(sym) > 5 and not sym[5].isdigit():
            continue
            
        # 3. Must contain the strike and end with CE/PE
        if strike_str in sym and sym.endswith(option_type):
            matches.append(sym)
            
    if not matches:
        return None
        
    def parse_expiry(sym):
        try:
            # Date starts after base_name and is 7 chars long (e.g., 16JUN26)
            date_str = sym[len(base_name):len(base_name)+7]
            return datetime.datetime.strptime(date_str, "%d%b%y").date()
        except:
            return datetime.date.max

    today = datetime.date.today()
    valid_contracts = []
    for m in matches:
        exp = parse_expiry(m)
        if exp >= today:
            valid_contracts.append((m, exp))
            
    if not valid_contracts:
        # Fallback to alphabetical sorting if parsing fails or all are in the past
        return sorted(matches)[0]
        
    # Sort by expiry date ascending (nearest first)
    valid_contracts.sort(key=lambda x: x[1])
    return valid_contracts[0][0]
