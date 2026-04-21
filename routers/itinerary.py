"""Itinerary endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/api/itinerary", tags=["itinerary"])


class ItineraryUpdate(BaseModel):
    real_expense: float | None = None
    status: str | None = None
    notes: str | None = None
    activity: str | None = None
    estimated_expense: float | None = None


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "day_number": row["day_number"],
        "date": row["date"],
        "city": row["city"],
        "activity": row["activity"],
        "estimated_expense": row["estimated_expense"],
        "real_expense": row["real_expense"],
        "status": row["status"],
        "notes": row["notes"],
    }


@router.get("")
def list_itinerary():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM itinerary ORDER BY day_number"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@router.get("/{day}")
def get_day(day: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM itinerary WHERE day_number = ?", (day,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Day not found")
        return _row_to_dict(row)


@router.put("/{day}")
def update_day(day: int, payload: ItineraryUpdate):
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [day]

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE itinerary SET {set_clause} WHERE day_number = ?", values
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Day not found")
        row = conn.execute(
            "SELECT * FROM itinerary WHERE day_number = ?", (day,)
        ).fetchone()
        return _row_to_dict(row)
