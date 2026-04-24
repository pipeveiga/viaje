"""Generate iCalendar (.ics) content for TripDesk events.

We generate two kinds of outputs:
- A single-event .ics attachment that gets sent via Telegram right after the
  user confirms a ticket/activity. On iPhone, tapping it opens the "Agregar
  al calendario" sheet.
- A full feed with every dated event in the trip (checklist items with
  parseable dates, itinerary days, confirmed documents) that Felipe can
  subscribe to from Settings → Calendar → Cuentas → Agregar cuenta → Otra →
  Agregar calendario suscrito. The feed auto-updates as things change.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Iterable


_MONTH_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _escape(text: str | None) -> str:
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding: break long lines every 75 octets."""
    if len(line) <= 75:
        return line
    parts = [line[:75]]
    i = 75
    while i < len(line):
        parts.append(" " + line[i : i + 74])
        i += 74
    return "\r\n".join(parts)


def _ics_datetime_utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _ics_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _stable_uid(seed: str) -> str:
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]
    return f"{h}@tripdesk.app"


def _build_event_lines(
    uid: str,
    summary: str,
    dtstart: date | datetime,
    dtend: date | datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    url: str | None = None,
) -> list[str]:
    now = _ics_datetime_utc(datetime.utcnow())
    lines = [
        "BEGIN:VEVENT",
        _fold(f"UID:{uid}"),
        f"DTSTAMP:{now}",
    ]

    if isinstance(dtstart, datetime):
        lines.append(f"DTSTART:{_ics_datetime_utc(dtstart)}")
        if dtend is None:
            dtend = dtstart + timedelta(hours=1)
        lines.append(f"DTEND:{_ics_datetime_utc(dtend)}")
    else:
        lines.append(f"DTSTART;VALUE=DATE:{_ics_date(dtstart)}")
        if dtend is None:
            dtend = dtstart + timedelta(days=1)
        if isinstance(dtend, datetime):
            dtend = dtend.date()
        lines.append(f"DTEND;VALUE=DATE:{_ics_date(dtend)}")

    lines.append(_fold(f"SUMMARY:{_escape(summary)}"))
    if description:
        lines.append(_fold(f"DESCRIPTION:{_escape(description)}"))
    if location:
        lines.append(_fold(f"LOCATION:{_escape(location)}"))
    if url:
        lines.append(_fold(f"URL:{_escape(url)}"))
    lines.append("END:VEVENT")
    return lines


def _wrap_calendar(event_blocks: Iterable[list[str]], name: str = "TripDesk · Europa 2026") -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TripDesk//Europa 2026//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape(name)}"),
        "X-WR-TIMEZONE:Europe/Madrid",
    ]
    for block in event_blocks:
        lines.extend(block)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def build_single_event_ics(
    summary: str,
    event_date: str,
    description: str | None = None,
    location: str | None = None,
    time_hm: str | None = None,
    duration_hours: float = 1.5,
    uid_seed: str | None = None,
) -> str:
    """Return the .ics text for a single event. `event_date` is YYYY-MM-DD.
    If `time_hm` ("HH:MM") is given, a 1.5h timed event; else all-day.
    """
    d = date.fromisoformat(event_date)
    if time_hm:
        try:
            h, m = time_hm.split(":")
            dt_start = datetime(d.year, d.month, d.day, int(h), int(m))
            dt_end = dt_start + timedelta(hours=duration_hours)
            start, end = dt_start, dt_end
        except (ValueError, IndexError):
            start, end = d, d + timedelta(days=1)
    else:
        start, end = d, d + timedelta(days=1)

    uid = _stable_uid(uid_seed or f"{summary}|{event_date}|{time_hm or ''}")
    block = _build_event_lines(
        uid=uid,
        summary=summary,
        dtstart=start,
        dtend=end,
        description=description,
        location=location,
    )
    return _wrap_calendar([block], name=summary[:60])


# ---- Trip feed builders -----------------------------------------------------

def parse_detail_date(detail: str | None) -> tuple[date | None, str | None]:
    """Try to parse the first date mentioned in a checklist detail text.
    Returns (date, optional HH:MM). Examples understood:
      "28-jul 21:45"           → (2026-07-28, "21:45")
      "3-ago"                  → (2026-08-03, None)
      "29-31 jul"              → (2026-07-29, None) [start only]
      "3-6 ago"                → (2026-08-03, None)
      "31-jul 18:10"           → (2026-07-31, "18:10")
    """
    if not detail:
        return None, None
    text = detail.lower()
    m = re.search(r"(\d{1,2})(?:-\d{1,2})?\s*[-\s]\s*([a-zá]+)", text)
    if not m:
        return None, None
    day = int(m.group(1))
    month_name = m.group(2)[:3]
    month = _MONTH_ES.get(month_name)
    if not month:
        return None, None
    try:
        d = date(2026, month, day)
    except ValueError:
        return None, None
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    hm = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    return d, hm


def _detail_end_date(detail: str | None) -> date | None:
    """For hotel-like details "29-31 jul" or "3-6 ago", return the END date."""
    if not detail:
        return None
    text = detail.lower()
    m = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})\s*([a-zá]+)", text)
    if not m:
        return None
    end_day = int(m.group(2))
    month_name = m.group(3)[:3]
    month = _MONTH_ES.get(month_name)
    if not month:
        return None
    try:
        return date(2026, month, end_day)
    except ValueError:
        return None


def _event_block_for_checklist(item: dict) -> list[str] | None:
    d, hm = parse_detail_date(item.get("detail"))
    if not d:
        return None
    concept = item.get("concept") or "Evento"
    amount = item.get("amount_eur")
    status = item.get("status")
    code = item.get("reservation_code")

    description_parts = [item.get("detail") or ""]
    if amount is not None:
        description_parts.append(f"€{float(amount):.2f}")
    if code:
        description_parts.append(f"Cod reserva: {code}")
    description_parts.append(
        f"Estado: {'pagado' if status == 'paid' else 'pendiente'}"
    )

    end_d = _detail_end_date(item.get("detail"))
    concept_lower = concept.lower()
    is_hotel = "hotel" in concept_lower
    if is_hotel and end_d and end_d > d:
        # Hotel estadía: evento all-day desde check-in hasta check-out.
        block = _build_event_lines(
            uid=_stable_uid(f"checklist|{item['id']}"),
            summary=concept,
            dtstart=d,
            dtend=end_d,
            description="\n".join(description_parts),
        )
        return block

    block = _build_event_lines(
        uid=_stable_uid(f"checklist|{item['id']}"),
        summary=concept,
        dtstart=datetime.combine(d, datetime.strptime(hm, "%H:%M").time()) if hm else d,
        description="\n".join(description_parts),
    )
    return block


def _event_block_for_document(doc: dict) -> list[str] | None:
    if not doc.get("date"):
        return None
    try:
        d = date.fromisoformat(doc["date"])
    except (ValueError, TypeError):
        return None
    summary = doc.get("description") or doc.get("filename") or "Comprobante"
    parts = []
    if doc.get("doc_type"):
        parts.append(doc["doc_type"])
    if doc.get("provider"):
        parts.append(doc["provider"])
    if doc.get("amount_eur") is not None:
        parts.append(f"€{float(doc['amount_eur']):.2f}")
    description = " · ".join(parts)
    return _build_event_lines(
        uid=_stable_uid(f"document|{doc['id']}"),
        summary=summary,
        dtstart=d,
        description=description,
    )


def _event_block_for_itinerary_day(day: dict) -> list[str] | None:
    if not day.get("date"):
        return None
    try:
        d = date.fromisoformat(day["date"])
    except (ValueError, TypeError):
        return None
    city = day.get("city") or ""
    activity = (day.get("activity") or "").strip()
    summary = f"{city} · día {day['day_number']}" if city else f"Día {day['day_number']}"
    description = activity or f"Día {day['day_number']} del viaje"
    return _build_event_lines(
        uid=_stable_uid(f"itinerary|{day['day_number']}"),
        summary=summary,
        dtstart=d,
        description=description,
        location=city,
    )


def build_trip_feed(
    checklist: list[dict],
    itinerary: list[dict],
    documents: list[dict],
    include_itinerary: bool = True,
) -> str:
    """Build the full .ics feed with every dated event in the trip."""
    blocks: list[list[str]] = []

    for item in checklist or []:
        block = _event_block_for_checklist(item)
        if block:
            blocks.append(block)

    if include_itinerary:
        for day in itinerary or []:
            block = _event_block_for_itinerary_day(day)
            if block:
                blocks.append(block)

    for doc in documents or []:
        if not doc.get("confirmed"):
            continue
        # Si el doc tiene checklist_id, el evento del checklist ya cubre la
        # fecha. Evitamos duplicar.
        if doc.get("checklist_id"):
            continue
        block = _event_block_for_document(doc)
        if block:
            blocks.append(block)

    return _wrap_calendar(blocks)
