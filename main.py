"""TripDesk — FastAPI entry point.

Run:
    python main.py --init-db      # create SQLite DB and seed data
    uvicorn main:app --reload     # start server
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db, reset_paid_items
from routers import checklist, config, documents, itinerary, summary

app = FastAPI(title="TripDesk", version="1.0.0")

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


@app.on_event("startup")
def _startup() -> None:
    init_db()


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
