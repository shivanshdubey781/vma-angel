from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import uvicorn
import logging
import asyncio

from .database import connect_to_mongo, close_mongo_connection, get_db
from .auth.router import router as auth_router
from .config_api.router import router as config_router
from .algo.router import router as algo_router
from .algo.instruments import load_instruments

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

app = FastAPI(title="VMA Algo Trading System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    await connect_to_mongo()
    await load_instruments()
    from .scheduler import run_scheduler, run_auto_token_generator
    asyncio.create_task(run_scheduler())
    asyncio.create_task(run_auto_token_generator())

@app.on_event("shutdown")
async def shutdown_event():
    await close_mongo_connection()

from .algo.history import router as history_router

app.include_router(auth_router)
app.include_router(config_router)
app.include_router(algo_router)
app.include_router(history_router)

BASE_DIR = Path(__file__).resolve().parent.parent

# Serve frontend
app.mount("/", StaticFiles(directory=str(BASE_DIR / "frontend"), html=True), name="frontend")

if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
