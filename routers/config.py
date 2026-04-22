"""Config endpoints (exchange rate, trip metadata)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from database import get_db, set_config

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    eur_usd_rate: float | None = None
    usd_ars_rate: float | None = None
    trip_name: str | None = None
    traveler: str | None = None


@router.get("")
def list_config():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


@router.put("")
def update_config(payload: ConfigUpdate):
    fields = payload.model_dump(exclude_none=True)
    for key, value in fields.items():
        set_config(key, str(value))
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}
