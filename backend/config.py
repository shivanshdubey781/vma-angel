import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
if (BASE_DIR / ".env").exists():
    load_dotenv(BASE_DIR / ".env")
else:
    load_dotenv(BASE_DIR.parent / ".env")

# ── MongoDB ──────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "VMA")

OHLC_MONGO_URI = os.getenv("OHLC_MONGO_URI", MONGO_URI)
OHLC_MONGO_DB = os.getenv("OHLC_MONGO_DB", MONGO_DB)

OHLC_MONGO_URI_1MIN = os.getenv("OHLC_MONGO_URI_1MIN", OHLC_MONGO_URI)
OHLC_MONGO_DB_1MIN = os.getenv("OHLC_MONGO_DB_1MIN", OHLC_MONGO_DB)
OHLC_MONGO_COLLECTION_1MIN = os.getenv("OHLC_MONGO_COLLECTION_1MIN", "OHLC")

OHLC_MONGO_URI_3MIN = os.getenv("OHLC_MONGO_URI_3MIN", OHLC_MONGO_URI)
OHLC_MONGO_DB_3MIN = os.getenv("OHLC_MONGO_DB_3MIN", OHLC_MONGO_DB)
OHLC_MONGO_COLLECTION_3MIN = os.getenv("OHLC_MONGO_COLLECTION_3MIN", "OHLC3")

OHLC_MONGO_URI_5MIN = os.getenv("OHLC_MONGO_URI_5MIN", OHLC_MONGO_URI)
OHLC_MONGO_DB_5MIN = os.getenv("OHLC_MONGO_DB_5MIN", OHLC_MONGO_DB)
OHLC_MONGO_COLLECTION_5MIN = os.getenv("OHLC_MONGO_COLLECTION_5MIN", "OHLC5")

# ── AngelOne SmartAPI ─────────────────────────────────────
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_MPIN = os.getenv("ANGEL_MPIN", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

# ── JWT (internal dashboard auth) ────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "supersecretkey")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 1440  # 24 hours
