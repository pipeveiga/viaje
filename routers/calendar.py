"""Subscribable .ics calendar feed for the trip.

On iPhone: Ajustes → Calendario → Cuentas → Añadir cuenta → Otra →
"Añadir calendario suscrito" y pegar la URL pública
https://<tu-app>.up.railway.app/api/calendar.ics
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from calendar_ics import build_trip_feed
from database import get_db

router = APIRouter(prefix="/api", tags=["calendar"])


def _rows_as_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


@router.get("/calendar.ics")
def trip_feed():
    with get_db() as conn:
        checklist = _rows_as_dicts(
            conn.execute("SELECT * FROM checklist ORDER BY id").fetchall()
        )
        itinerary = _rows_as_dicts(
            conn.execute("SELECT * FROM itinerary ORDER BY day_number").fetchall()
        )
        documents = _rows_as_dicts(
            conn.execute(
                "SELECT * FROM documents WHERE confirmed = 1"
            ).fetchall()
        )
    body = build_trip_feed(checklist, itinerary, documents, include_itinerary=True)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="tripdesk.ics"',
            "Cache-Control": "public, max-age=300",
        },
    )
