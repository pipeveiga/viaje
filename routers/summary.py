"""Aggregate financial summary endpoint."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from database import get_config, get_db

router = APIRouter(prefix="/api", tags=["summary"])


def _current_trip_day(start: str, end: str) -> dict:
    today = date.today()
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    total_days = (end_d - start_d).days + 1

    if today < start_d:
        return {
            "days_until": (start_d - today).days,
            "current_day": None,
            "total_days": total_days,
            "in_trip": False,
        }
    if today > end_d:
        return {
            "days_until": 0,
            "current_day": None,
            "total_days": total_days,
            "in_trip": False,
        }
    return {
        "days_until": 0,
        "current_day": (today - start_d).days + 1,
        "total_days": total_days,
        "in_trip": True,
    }


@router.get("/summary")
def summary():
    rate = float(get_config("eur_usd_rate", "1.172") or "1.172")
    usd_ars = float(get_config("usd_ars_rate", "1450") or "1450")
    start = get_config("start_date", "2026-07-25") or "2026-07-25"
    end = get_config("end_date", "2026-08-15") or "2026-08-15"

    with get_db() as conn:
        itinerary_rows = conn.execute(
            """SELECT day_number, date, city, activity, estimated_expense,
                      real_expense, status
               FROM itinerary ORDER BY day_number"""
        ).fetchall()
        checklist_rows = conn.execute(
            "SELECT status, amount_eur FROM checklist"
        ).fetchall()

    estimated_total = sum(r["estimated_expense"] or 0 for r in itinerary_rows)
    real_total = sum(r["real_expense"] or 0 for r in itinerary_rows)

    paid_total = sum(
        (r["amount_eur"] or 0) for r in checklist_rows if r["status"] == "paid"
    )
    pending_total = sum(
        (r["amount_eur"] or 0) for r in checklist_rows if r["status"] == "pending"
    )

    budget_total_eur = estimated_total + paid_total + pending_total
    trip_info = _current_trip_day(start, end)

    current_city = None
    if trip_info["in_trip"] and trip_info["current_day"]:
        for r in itinerary_rows:
            if r["day_number"] == trip_info["current_day"]:
                current_city = r["city"]
                break

    def to_usd(v: float) -> float:
        return round(v * rate, 2)

    return {
        "eur_usd_rate": rate,
        "usd_ars_rate": usd_ars,
        "trip": {
            **trip_info,
            "start_date": start,
            "end_date": end,
            "current_city": current_city,
        },
        "budget": {
            "estimated_eur": round(estimated_total, 2),
            "estimated_usd": to_usd(estimated_total),
            "paid_eur": round(paid_total, 2),
            "paid_usd": to_usd(paid_total),
            "pending_eur": round(pending_total, 2),
            "pending_usd": to_usd(pending_total),
            "spent_eur": round(real_total, 2),
            "spent_usd": to_usd(real_total),
            "total_eur": round(budget_total_eur, 2),
            "total_usd": to_usd(budget_total_eur),
        },
    }
