"""TripDesk — FastAPI entry point.

Run:
    python main.py --init-db      # create SQLite DB and seed data
    uvicorn main:app --reload     # start server + Telegram bot (same process)

The Telegram bot is launched as a background task inside the FastAPI lifespan,
so a single Railway service runs both the web app and the bot.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db, reset_paid_items
from routers import checklist, config, documents, itinerary, summary

logger = logging.getLogger("tripdesk")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def _run_bot_forever() -> None:
    """Launch the Telegram bot inside the FastAPI event loop.

    If TELEGRAM_BOT_TOKEN is missing the bot simply logs a warning and returns
    so the web server keeps running normally.
    """
    try:
        from bot import build_app as build_bot_app
        application = build_bot_app()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram bot not started: %s", exc)
        return

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram bot polling started")
        # Keep the task alive until it's cancelled on shutdown.
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("Telegram bot crashed")
    finally:
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bot_task = asyncio.create_task(_run_bot_forever())
    try:
        yield
    finally:
        if not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="TripDesk", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(itinerary.router)
app.include_router(checklist.router)
app.include_router(documents.router)
app.include_router(config.router)
app.include_router(summary.router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="TripDesk utilities")
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create tables and seed initial data, then exit.",
    )
    parser.add_argument(
        "--reset-paid",
        action="store_true",
        help="Mark all paid checklist items as pending (clears amounts), then exit.",
    )
    args = parser.parse_args()

    if args.init_db:
        init_db()
        print(f"Database initialized at {os.getenv('DATABASE_PATH', 'tripdesk.db')}")
        sys.exit(0)

    if args.reset_paid:
        n = reset_paid_items()
        print(f"Reset {n} checklist items to pending.")
        sys.exit(0)

    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    _cli()
