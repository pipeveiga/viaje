"""Checklist endpoints for upfront payments."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/api/checklist", tags=["checklist"])


class ChecklistUpdate(BaseModel):
    status: str | None = None
    amount_eur: float | None = None
    detail: str | None = None
    reservation_code: str | None = None


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "concept": row["concept"],
        "detail": row["detail"],
        "amount_eur": row["amount_eur"],
        "deadline": row["deadline"],
        "status": row["status"],
        "paid_date": row["paid_date"],
        "reservation_code": row["reservation_code"],
    }


@router.get("")
def list_checklist():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM checklist ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]


@router.put("/{item_id}")
def update_item(item_id: int, payload: ChecklistUpdate):
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    if fields.get("status") == "paid" and "paid_date" not in fields:
        fields["paid_date"] = date.today().isoformat()
    elif fields.get("status") == "pending":
        fields["paid_date"] = None

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [item_id]

    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE checklist SET {set_clause} WHERE id = ?", values
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")
        row = conn.execute(
            "SELECT * FROM checklist WHERE id = ?", (item_id,)
        ).fetchone()
        return _row_to_dict(row)
