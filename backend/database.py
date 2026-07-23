from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
import logging

from .config import MONGO_URI, MONGO_DB

logger = logging.getLogger("vma.database")

class Database:
    client: AsyncIOMotorClient = None
    sync_client: MongoClient = None
    db = None
    sync_db = None

db_instance = Database()

async def connect_to_mongo():
    logger.info("Connecting to MongoDB...")
    db_instance.client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db_instance.db = db_instance.client[MONGO_DB]
    db_instance.sync_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db_instance.sync_db = db_instance.sync_client[MONGO_DB]
    
    # Initialize indexes
    await db_instance.db.sessions.create_index("token", unique=True)
    await db_instance.db.trades.create_index("trade_id", unique=True)
    logger.info("MongoDB connected and indexes verified.")

async def close_mongo_connection():
    if db_instance.client:
        db_instance.client.close()
    if db_instance.sync_client:
        db_instance.sync_client.close()

def get_db():
    return db_instance.db
    
def get_sync_db():
    return db_instance.sync_db
