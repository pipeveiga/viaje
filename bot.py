"""Telegram bot for TripDesk.

Run in a separate process alongside the FastAPI server:
    python bot.py

The bot only responds to the chat id configured in TELEGRAM_CHAT_ID.

Behavior:
- Commands /start /resumen /hoy /pendientes — quick lookups.
- Photo or PDF → sent to OpenAI for extraction; waits for SI/NO confirmation
  before persisting.
- Any other text (that isn't a pending SI/NO) is treated as a free-form
  question and answered by OpenAI with the full trip context as prompt.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_processor import answer_question, classify_intent, process_document
from database import get_config, get_db, init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
UPLOADS_PATH = os.getenv("UPLOADS_PATH", "uploads/")


def _is_authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return False
    chat = update.effective_chat
    return chat is not None and str(chat.id) == ALLOWED_CHAT_ID


async def _guard(update: Update) -> bool:
    if _is_authorized(update):
        return True
    if update.effective_chat:
        logger.warning("Ignored message from chat_id=%s", update.effective_chat.id)
    return False


# -- commands ---------------------------------------------------------------


HELP_TEXT = (
    "✈️ Hola Feli! Soy *TripDesk*, te acompaño a armar Europa 2026.\n\n"
    "Mandame todo lo que vayas pagando, reservando o encontrando:\n"
    "📎 *Fotos o PDFs* (tickets, facturas, reservas) y los cargo solos.\n"
    "💬 O contame con tus palabras — ej: _\"reservé el Coliseo para el 30-jul\"_,"
    " _\"pagué €450 del hotel de Roma\"_, _\"quiero ir a Montjuïc el 10-ago\"_.\n"
    "❓ También me podés preguntar cualquier cosa del viaje y te contesto con"
    " lo que ya cargamos.\n\n"
    "Atajos útiles: /resumen · /hoy · /pendientes.\n\n"
    "Cuando te proponga algo (guardar, marcar pagado, agregar actividad) te"
    " pregunto SI/NO antes de tocar nada. Dale cuando quieras 🚀"
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_resumen(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    rate = float(get_config("eur_usd_rate", "1.172") or "1.172")
    with get_db() as conn:
        it = conn.execute(
            "SELECT estimated_expense, real_expense FROM itinerary"
        ).fetchall()
        ch = conn.execute("SELECT status, amount_eur FROM checklist").fetchall()

    estimated = sum(r["estimated_expense"] or 0 for r in it)
    spent = sum(r["real_expense"] or 0 for r in it)
    paid = sum((r["amount_eur"] or 0) for r in ch if r["status"] == "paid")
    pending = sum((r["amount_eur"] or 0) for r in ch if r["status"] == "pending")
    total = estimated + paid + pending

    def fmt(v: float) -> str:
        return f"€{v:,.2f} (US${v*rate:,.2f})"

    text = (
        "💰 *Resumen financiero*\n\n"
        f"Total estimado: {fmt(total)}\n"
        f"Ya pagado: {fmt(paid)}\n"
        f"Pendiente: {fmt(pending)}\n"
        f"Gastado en viaje: {fmt(spent)}\n"
        f"Itinerario estimado: {fmt(estimated)}\n\n"
        f"Tipo de cambio EUR/USD: {rate}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_hoy(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    today = date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM itinerary WHERE date = ?", (today,)
        ).fetchone()
    if not row:
        await update.message.reply_text(
            "No hay plan para hoy en el itinerario (probablemente el viaje no empezó o ya terminó)."
        )
        return
    parts = [
        f"📅 *Día {row['day_number']}* — {row['date']}",
        f"📍 {row['city']}",
    ]
    if row["activity"]:
        parts.append(f"🎯 {row['activity']}")
    parts.append(f"💶 Estimado: €{row['estimated_expense']:.2f}")
    if row["real_expense"] is not None:
        parts.append(f"💰 Real: €{row['real_expense']:.2f}")
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def cmd_pendientes(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    with get_db() as conn:
        rows = conn.execute(
            "SELECT concept, detail, amount_eur FROM checklist WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    if not rows:
        await update.message.reply_text("🎉 No hay pagos pendientes.")
        return
    lines = ["📋 *Pagos pendientes:*\n"]
    for r in rows:
        amount = f" — €{r['amount_eur']:.2f}" if r["amount_eur"] is not None else ""
        lines.append(f"• {r['concept']}{amount}")
        if r["detail"]:
            lines.append(f"  _{r['detail']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# -- document processing ----------------------------------------------------


def _save_pending(chat_id: str, data: dict) -> int:
    # Default kind for backwards compatibility with existing document payloads.
    data.setdefault("kind", "document")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO pending_confirmations (chat_id, data_json) VALUES (?, ?)",
            (str(chat_id), json.dumps(data)),
        )
        return cur.lastrowid


def _consume_pending(chat_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, data_json FROM pending_confirmations
               WHERE chat_id = ? ORDER BY id DESC LIMIT 1""",
            (str(chat_id),),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_confirmations WHERE id = ?", (row["id"],))
        return json.loads(row["data_json"])


def _peek_pending(chat_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT data_json FROM pending_confirmations
               WHERE chat_id = ? ORDER BY id DESC LIMIT 1""",
            (str(chat_id),),
        ).fetchone()
    return json.loads(row["data_json"]) if row else None


def _record_turn(chat_id: str, role: str, content: str) -> None:
    if not content:
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversation_turns (chat_id, role, content) VALUES (?, ?, ?)",
            (str(chat_id), role, content),
        )
        # Mantener solo las últimas 40 entradas por chat para que no crezca.
        conn.execute(
            """DELETE FROM conversation_turns
               WHERE chat_id = ?
                 AND id NOT IN (
                   SELECT id FROM conversation_turns
                   WHERE chat_id = ? ORDER BY id DESC LIMIT 40
                 )""",
            (str(chat_id), str(chat_id)),
        )


def _recent_turns(chat_id: str, n: int = 8) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT role, content FROM conversation_turns
               WHERE chat_id = ? ORDER BY id DESC LIMIT ?""",
            (str(chat_id), n),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _normalize(s: str) -> str:
    """Lowercase + strip accents + collapse whitespace + drop arrow chars."""
    import unicodedata

    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("→", " ").replace("->", " ").replace("/", " ")
    return " ".join(s.split())


def _match_checklist(name_hint: str | None, include_paid: bool = False) -> int | None:
    if not name_hint:
        return None
    with get_db() as conn:
        if include_paid:
            rows = conn.execute("SELECT id, concept, status FROM checklist").fetchall()
        else:
            rows = conn.execute(
                "SELECT id, concept, status FROM checklist WHERE status = 'pending'"
            ).fetchall()

    hint = _normalize(name_hint)
    hint_tokens = set(hint.split())

    # 1) Match exacto normalizado o substring
    for r in rows:
        concept_n = _normalize(r["concept"])
        if concept_n == hint or concept_n in hint or hint in concept_n:
            return r["id"]

    # 2) Scoring por tokens en común (ignorando stopwords cortas)
    stop = {"de", "del", "la", "el", "los", "las", "y", "o", "a", "en"}
    best, best_score = None, 0
    for r in rows:
        concept_n = _normalize(r["concept"])
        concept_tokens = set(t for t in concept_n.split() if t not in stop and len(t) > 2)
        hint_significant = set(t for t in hint_tokens if t not in stop and len(t) > 2)
        score = len(concept_tokens & hint_significant)
        if score > best_score:
            best, best_score = r["id"], score
    return best if best_score >= 2 else None


def _match_checklist_by_metadata(
    doc_type: str | None, day_number: int | None, city: str | None, description: str | None
) -> int | None:
    """Fallback cuando la IA no devolvió coincide_checklist: intenta ubicar
    el item por tipo de documento y día/ciudad del viaje.
    """
    if not doc_type:
        return None
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, concept, detail, status FROM checklist WHERE status = 'pending'"
        ).fetchall()
    dt = doc_type.lower()
    city_n = _normalize(city or "")
    day = str(day_number) if day_number else None

    candidates = []
    for r in rows:
        concept_n = _normalize(r["concept"])
        detail_n = _normalize(r["detail"] or "")
        score = 0
        if dt == "hotel" and "hotel" in concept_n:
            score += 2
        if dt in ("vuelo",) and "vuelo" in concept_n:
            score += 2
        if dt == "transporte" and any(w in concept_n for w in ("tren", "bus", "t-jove", "metro")):
            score += 2
        if dt == "museo_entrada" and any(
            w in concept_n for w in ("museo", "tour", "coliseo", "bernabeu", "montserrat")
        ):
            score += 2
        if city_n and city_n in concept_n:
            score += 2
        if city_n and city_n in detail_n:
            score += 1
        if day and day in detail_n:
            score += 1
        if score > 0:
            candidates.append((score, r["id"]))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    # Exigir al menos score 3 para evitar falsos positivos.
    return candidates[0][1] if candidates[0][0] >= 3 else None


def _get_rates() -> tuple[float, float]:
    eur_usd = float(get_config("eur_usd_rate", "1.172") or "1.172")
    usd_ars = float(get_config("usd_ars_rate", "1450") or "1450")
    return eur_usd, usd_ars


def _convert_amounts(
    amount: float | None, currency: str | None
) -> tuple[float | None, float | None]:
    """Convert an amount in its original currency to (EUR, USD) using config rates."""
    if amount is None:
        return None, None
    cur = (currency or "").upper()
    eur_usd, usd_ars = _get_rates()
    if cur == "EUR":
        return round(float(amount), 2), round(float(amount) * eur_usd, 2)
    if cur == "USD":
        return round(float(amount) / eur_usd, 2), round(float(amount), 2)
    if cur == "ARS":
        usd = float(amount) / usd_ars
        eur = usd / eur_usd
        return round(eur, 2), round(usd, 2)
    # unknown currency: assume it's already EUR so we don't explode
    return round(float(amount), 2), round(float(amount) * eur_usd, 2)


def _find_duplicate(reservation_number: str | None) -> dict | None:
    """Find a previously confirmed document with the same reservation_number."""
    if not reservation_number:
        return None
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, description, amount_eur, amount_usd, checklist_id
               FROM documents
               WHERE confirmed = 1 AND reservation_number = ?
               ORDER BY id DESC LIMIT 1""",
            (reservation_number,),
        ).fetchone()
    return dict(row) if row else None


def _append_activity(conn, day_number: int, description: str, amount_eur: float | None) -> None:
    """Append a short activity line to the day's activity column and bump
    the estimated_expense by amount_eur (if provided).
    """
    row = conn.execute(
        "SELECT activity, estimated_expense FROM itinerary WHERE day_number = ?",
        (day_number,),
    ).fetchone()
    if not row:
        return
    current_text = row["activity"] or ""
    label = description.strip()
    if amount_eur:
        label = f"{label} (€{amount_eur:.2f})"
    new_text = f"{current_text}\n• {label}".strip() if current_text else f"• {label}"
    new_estimated = float(row["estimated_expense"] or 0) + float(amount_eur or 0)
    conn.execute(
        """UPDATE itinerary
           SET activity = ?, estimated_expense = ?
           WHERE day_number = ?""",
        (new_text, new_estimated, day_number),
    )


async def handle_document_or_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _guard(update):
        return

    msg = update.message
    await context.bot.send_chat_action(
        chat_id=msg.chat_id, action=ChatAction.TYPING
    )

    file_obj = None
    filename = ""
    if msg.photo:
        photo = msg.photo[-1]
        file_obj = await photo.get_file()
        filename = f"{photo.file_unique_id}.jpg"
    elif msg.document:
        file_obj = await msg.document.get_file()
        filename = msg.document.file_name or f"{msg.document.file_unique_id}.bin"
    else:
        await msg.reply_text("No detecté foto ni documento en el mensaje.")
        return

    Path(UPLOADS_PATH).mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix or ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = Path(UPLOADS_PATH) / unique_name
    await file_obj.download_to_drive(custom_path=str(dest))
    file_bytes = dest.read_bytes()

    with get_db() as conn:
        checklist_rows = conn.execute(
            "SELECT id, concept, status FROM checklist ORDER BY id"
        ).fetchall()
        itinerary_rows = conn.execute(
            "SELECT day_number, date, city, activity FROM itinerary ORDER BY day_number"
        ).fetchall()
    checklist_items = [dict(r) for r in checklist_rows]
    itinerary_days = [dict(r) for r in itinerary_rows]

    try:
        extracted = process_document(
            file_bytes, filename, checklist_items, itinerary_days
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI processing failed")
        await msg.reply_text(f"⚠️ Error procesando el documento: {exc}")
        return

    is_roundtrip = bool(extracted.get("es_ida_vuelta"))
    currency = (extracted.get("moneda") or "").upper() or None
    per_leg_orig = extracted.get("monto_original")
    total_orig = extracted.get("monto_total_original")
    if not is_roundtrip and per_leg_orig is None:
        per_leg_orig = total_orig
    if not is_roundtrip and total_orig is None:
        total_orig = per_leg_orig

    # Red de seguridad: si la IA detectó ida y vuelta pero devolvió el mismo
    # número como total y como tramo (típico cuando el ticket no muestra
    # desglose), dividimos por 2 acá.
    if is_roundtrip and total_orig is not None:
        if per_leg_orig is None or abs(float(per_leg_orig) - float(total_orig)) < 0.01:
            per_leg_orig = round(float(total_orig) / 2, 2)

    per_leg_eur, per_leg_usd = _convert_amounts(per_leg_orig, currency)
    total_eur, total_usd = _convert_amounts(total_orig, currency)

    # Duplicate detection: same reservation_number already confirmed, or the
    # matched checklist item is already paid (ticket + factura enviados por separado).
    reservation_number = extracted.get("numero_reserva")
    dup = _find_duplicate(reservation_number)

    checklist_id = _match_checklist(
        extracted.get("coincide_checklist"), include_paid=bool(dup)
    )
    checklist_id_vuelta = (
        _match_checklist(
            extracted.get("coincide_checklist_vuelta"), include_paid=bool(dup)
        )
        if is_roundtrip
        else None
    )

    # Fallback: si la IA no matcheó pero tenemos tipo + día, inferimos.
    if not checklist_id and not is_roundtrip:
        day_num = extracted.get("dia_viaje")
        day_city = None
        if day_num:
            for d in itinerary_days:
                if d["day_number"] == day_num:
                    day_city = d["city"]
                    break
        checklist_id = _match_checklist_by_metadata(
            extracted.get("tipo"),
            day_num,
            day_city,
            extracted.get("descripcion"),
        )

    is_duplicate = bool(dup)
    if not is_duplicate and checklist_id:
        with get_db() as conn:
            row = conn.execute(
                "SELECT status FROM checklist WHERE id = ?", (checklist_id,)
            ).fetchone()
            if row and row["status"] == "paid":
                is_duplicate = True

    payload = {
        "filename": filename,
        "file_path": str(dest),
        "doc_type": extracted.get("tipo"),
        "description": extracted.get("descripcion"),
        "currency": currency,
        "amount_original": per_leg_orig,
        "amount_total_original": total_orig,
        "amount_eur": per_leg_eur,
        "amount_total_eur": total_eur,
        "amount_usd": per_leg_usd,
        "amount_total_usd": total_usd,
        "date": extracted.get("fecha"),
        "provider": extracted.get("proveedor"),
        "reservation_number": reservation_number,
        "day_number": extracted.get("dia_viaje"),
        "is_roundtrip": is_roundtrip,
        "checklist_id": checklist_id,
        "checklist_id_vuelta": checklist_id_vuelta,
        "confidence": extracted.get("confianza"),
        "is_duplicate": is_duplicate,
        "duplicate_of_doc_id": dup["id"] if dup else None,
    }

    # Guardamos el documento en la DB como "no confirmado" apenas llega, así
    # aunque Felipe después corrija por texto o se olvide de contestar SI/NO,
    # la foto/PDF queda persistida y visible en el dashboard.
    doc_eur_pre = total_eur if is_roundtrip else per_leg_eur
    doc_usd_pre = total_usd if is_roundtrip else per_leg_usd
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO documents
               (filename, file_path, doc_type, description, amount_eur,
                amount_usd, date, provider, reservation_number, day_number,
                checklist_id, confidence, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                filename,
                str(dest),
                payload["doc_type"],
                payload["description"],
                doc_eur_pre,
                doc_usd_pre,
                payload["date"],
                payload["provider"],
                payload["reservation_number"],
                payload["day_number"],
                payload["checklist_id"],
                payload["confidence"],
            ),
        )
        payload["document_id"] = cur.lastrowid

    _save_pending(str(msg.chat_id), payload)

    def _fmt_orig(v: float | None) -> str:
        if v is None:
            return "s/d"
        if currency == "ARS":
            return f"AR${v:,.0f}"
        if currency == "USD":
            return f"US${v:,.2f}"
        if currency == "EUR":
            return f"€{v:,.2f}"
        return f"{v:,.2f} {currency or ''}".strip()

    def _fmt_eur_usd(eur: float | None, usd: float | None) -> str:
        if eur is None and usd is None:
            return "s/d"
        parts = []
        if eur is not None:
            parts.append(f"€{eur:,.2f}")
        if usd is not None:
            parts.append(f"US${usd:,.2f}")
        return " · ".join(parts)

    if is_roundtrip and per_leg_eur is not None and total_eur is not None:
        amount_str = (
            f"{_fmt_orig(total_orig)} total → {_fmt_orig(per_leg_orig)} por tramo (×2)\n"
            f"💱 Por tramo: {_fmt_eur_usd(per_leg_eur, per_leg_usd)}"
        )
    elif per_leg_eur is not None:
        if currency and currency != "EUR":
            amount_str = (
                f"{_fmt_orig(per_leg_orig)} → {_fmt_eur_usd(per_leg_eur, per_leg_usd)}"
            )
        else:
            amount_str = _fmt_eur_usd(per_leg_eur, per_leg_usd)
    else:
        amount_str = "s/d"

    concept_by_id = {c["id"]: c["concept"] for c in checklist_items}
    effect_lines = []
    if is_duplicate:
        dup_desc = dup["description"] if dup else None
        effect_lines.append(
            "🔁 Ya tenía este comprobante cargado"
            + (f" ({dup_desc})" if dup_desc else "")
            + ". Lo sumo como respaldo."
        )
        if checklist_id:
            name = concept_by_id.get(checklist_id)
            if name and per_leg_eur is not None:
                effect_lines.append(
                    f"📝 Completo el importe de *{name}* con €{per_leg_eur:.2f}"
                )
        if checklist_id_vuelta:
            name = concept_by_id.get(checklist_id_vuelta)
            if name and per_leg_eur is not None:
                effect_lines.append(
                    f"📝 Completo el importe de *{name}* con €{per_leg_eur:.2f}"
                )
    else:
        if checklist_id:
            name = concept_by_id.get(checklist_id)
            if name:
                effect_lines.append(f"✅ Marco *{name}* como pagado")
        if checklist_id_vuelta:
            name = concept_by_id.get(checklist_id_vuelta)
            if name:
                effect_lines.append(f"✅ Marco *{name}* como pagado")
        if payload["day_number"] and not is_roundtrip and not checklist_id:
            effect_lines.append(
                f"📅 Lo sumo al día {payload['day_number']} del itinerario"
            )

    effects_block = ("\n" + "\n".join(effect_lines)) if effect_lines else ""

    roundtrip_tag = " (ida y vuelta)" if is_roundtrip else ""

    summary = (
        f"Vi esto 👀\n"
        f"📄 {payload['description'] or 'documento'}{roundtrip_tag}\n"
        f"📆 {payload['date'] or 'sin fecha'}"
        f"{'  ·  🏷️ ' + payload['doc_type'] if payload['doc_type'] else ''}\n"
        f"💶 {amount_str}"
        f"{effects_block}\n\n"
        "¿Lo guardo? *SI* / *NO*"
    )
    _record_turn(str(msg.chat_id), "user", f"[comprobante enviado: {filename}]")
    _record_turn(str(msg.chat_id), "assistant", summary)
    await msg.reply_text(summary, parse_mode="Markdown")


async def _apply_confirmed_action(data: dict) -> str:
    """Persist a pending action previously proposed to the user.

    The payload's `kind` drives behavior:
    - "document": a receipt that the user wants saved.
    - "activity": add an activity to a day of the itinerary.
    - "mark_paid": mark a checklist item as paid, optionally with an amount.
    """
    kind = data.get("kind", "document")

    if kind == "activity":
        day = data.get("day_number")
        description = data.get("description") or "actividad"
        amount_eur = data.get("amount_eur")
        if not day:
            return "⚠️ No pude guardar la actividad: faltó el día."
        with get_db() as conn:
            _append_activity(conn, int(day), description, amount_eur)
        extra = f" (€{amount_eur:.2f})" if amount_eur else ""
        return f"Listo, anoté *{description}*{extra} en el día {day}. 📌"

    if kind == "mark_paid":
        cid = data.get("checklist_id")
        concept = data.get("concept") or "item"
        amount_eur = data.get("amount_eur")
        if not cid:
            return "⚠️ No encontré el item del checklist."
        today_iso = datetime.now().date().isoformat()
        with get_db() as conn:
            if amount_eur is not None:
                conn.execute(
                    """UPDATE checklist
                       SET status = 'paid', paid_date = ?, amount_eur = ?
                       WHERE id = ?""",
                    (today_iso, amount_eur, cid),
                )
            else:
                conn.execute(
                    """UPDATE checklist
                       SET status = 'paid', paid_date = ?
                       WHERE id = ?""",
                    (today_iso, cid),
                )
        extra = f" con €{amount_eur:.2f}" if amount_eur else ""
        return f"Dale, marqué *{concept}* como pagado{extra}. ✅"

    if kind == "update_checklist_amount":
        cid = data.get("checklist_id")
        concept = data.get("concept") or "item"
        amount_eur = data.get("amount_eur")
        if not cid or amount_eur is None:
            return "⚠️ Me faltó algún dato para corregir el importe."
        with get_db() as conn:
            conn.execute(
                "UPDATE checklist SET amount_eur = ? WHERE id = ?",
                (float(amount_eur), cid),
            )
        return f"Corregido: *{concept}* ahora figura en €{float(amount_eur):.2f}. ✏️"

    if kind == "mark_paid_roundtrip":
        cid_ida = data.get("checklist_id_ida")
        cid_vuelta = data.get("checklist_id_vuelta")
        per_leg = data.get("per_leg_eur")
        concept_ida = data.get("concept_ida") or "ida"
        concept_vuelta = data.get("concept_vuelta") or "vuelta"
        today_iso = datetime.now().date().isoformat()
        with get_db() as conn:
            for cid in (cid_ida, cid_vuelta):
                if not cid:
                    continue
                conn.execute(
                    """UPDATE checklist
                       SET status = 'paid', paid_date = ?, amount_eur = ?
                       WHERE id = ?""",
                    (today_iso, per_leg, cid),
                )
        return (
            f"Marqué *{concept_ida}* y *{concept_vuelta}* como pagados a "
            f"€{float(per_leg):.2f} c/u. ✅"
        )

    # -- document ----------------------------------------------------------
    today_iso = datetime.now().date().isoformat()
    per_leg_eur = data.get("amount_eur")
    per_leg_usd = data.get("amount_usd")
    total_eur = data.get("amount_total_eur")
    total_usd = data.get("amount_total_usd")
    is_roundtrip = bool(data.get("is_roundtrip"))
    is_duplicate = bool(data.get("is_duplicate"))
    dup_doc_id = data.get("duplicate_of_doc_id")
    existing_doc_id = data.get("document_id")

    doc_eur = total_eur if is_roundtrip else per_leg_eur
    doc_usd = total_usd if is_roundtrip else per_leg_usd

    with get_db() as conn:
        if existing_doc_id:
            # El doc se insertó no-confirmado cuando llegó la foto/PDF. Sólo
            # flip a confirmed=1 y refrescamos por si la IA devolvió algo
            # distinto al momento de confirmar.
            conn.execute(
                """UPDATE documents
                   SET doc_type = ?, description = ?, amount_eur = ?,
                       amount_usd = ?, date = ?, provider = ?,
                       reservation_number = ?, day_number = ?, checklist_id = ?,
                       confidence = ?, confirmed = 1
                   WHERE id = ?""",
                (
                    data["doc_type"],
                    data["description"],
                    doc_eur,
                    doc_usd,
                    data["date"],
                    data["provider"],
                    data["reservation_number"],
                    data["day_number"],
                    data["checklist_id"],
                    data["confidence"],
                    existing_doc_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO documents
                   (filename, file_path, doc_type, description, amount_eur,
                    amount_usd, date, provider, reservation_number, day_number,
                    checklist_id, confidence, confirmed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    data["filename"],
                    data["file_path"],
                    data["doc_type"],
                    data["description"],
                    doc_eur,
                    doc_usd,
                    data["date"],
                    data["provider"],
                    data["reservation_number"],
                    data["day_number"],
                    data["checklist_id"],
                    data["confidence"],
                ),
            )

        # Si el doc previo no tenía monto (p.ej. ticket sin precio), lo
        # completamos con el de este (suele ser la factura real).
        if is_duplicate and dup_doc_id and doc_eur is not None:
            conn.execute(
                """UPDATE documents
                   SET amount_eur = COALESCE(amount_eur, ?),
                       amount_usd = COALESCE(amount_usd, ?)
                   WHERE id = ?""",
                (doc_eur, doc_usd, dup_doc_id),
            )

        for cid in (data.get("checklist_id"), data.get("checklist_id_vuelta")):
            if not cid:
                continue
            if is_duplicate:
                # Mantener el status pagado del item; completar el importe si
                # el nuevo doc trae uno (y el anterior no lo tenía o era menor).
                if per_leg_eur is not None:
                    conn.execute(
                        """UPDATE checklist
                           SET amount_eur = ?,
                               status = 'paid',
                               paid_date = COALESCE(paid_date, ?)
                           WHERE id = ?""",
                        (per_leg_eur, today_iso, cid),
                    )
                continue
            if per_leg_eur is not None:
                conn.execute(
                    """UPDATE checklist
                       SET status = 'paid', paid_date = ?, amount_eur = ?
                       WHERE id = ?""",
                    (today_iso, per_leg_eur, cid),
                )
            else:
                conn.execute(
                    """UPDATE checklist SET status = 'paid', paid_date = ?
                       WHERE id = ?""",
                    (today_iso, cid),
                )

        # Si el doc matchea un día pero NO un item del checklist → es una
        # actividad del viaje, la sumamos al día.
        if (
            not is_roundtrip
            and not is_duplicate
            and not data.get("checklist_id")
            and data.get("day_number")
        ):
            _append_activity(
                conn,
                int(data["day_number"]),
                data.get("description") or "gasto",
                per_leg_eur,
            )

    if is_duplicate:
        return "Perfecto, lo guardé como respaldo y te completé el importe si faltaba. 🙌"
    return "Listo, lo guardé y actualicé la app 🙌"


SI_WORDS = {"si", "sí", "s", "yes", "y", "dale", "ok", "okay", "listo", "hazlo", "hacelo"}
NO_WORDS = {"no", "n", "cancel", "cancelar", "nada", "anula", "anulá"}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw = (update.message.text or "").strip()
    text = raw.lower()
    chat_id = str(update.message.chat_id)

    _record_turn(chat_id, "user", raw)

    has_pending = _peek_pending(chat_id) is not None

    if has_pending and text in SI_WORDS:
        data = _consume_pending(chat_id)
        reply = await _apply_confirmed_action(data)
        _record_turn(chat_id, "assistant", reply)
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    if has_pending and text in NO_WORDS:
        consumed = _consume_pending(chat_id)
        if consumed and consumed.get("kind", "document") == "document":
            try:
                Path(consumed["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            doc_id = consumed.get("document_id")
            if doc_id:
                with get_db() as conn:
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        reply = "Listo, lo descarto. Seguimos."
        _record_turn(chat_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    # Sin pending → SI/NO y todo lo demás van al modelo con historial.
    await _answer_free_text(update, context)


async def _answer_free_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await context.bot.send_chat_action(
        chat_id=update.message.chat_id, action=ChatAction.TYPING
    )
    chat_id = str(update.message.chat_id)
    history = _recent_turns(chat_id, n=8)
    # El último turn ya tiene el mensaje actual; lo sacamos para no duplicarlo.
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    try:
        context_block = _build_trip_context()
        result = classify_intent(update.message.text, context_block, history)
    except Exception as exc:  # noqa: BLE001
        logger.exception("intent classification failed")
        try:
            answer = answer_question(update.message.text, _build_trip_context())
        except Exception as exc2:  # noqa: BLE001
            logger.exception("Q&A fallback failed")
            await update.message.reply_text(f"⚠️ Se me trabó: {exc2}")
            return
        _record_turn(chat_id, "assistant", answer)
        await update.message.reply_text(answer)
        return

    reply = (result.get("reply") or "").strip()
    action = result.get("action")

    async def _reply_or_qa(fallback_reply: str) -> str:
        """Si el modelo contestó algo útil lo devolvemos; si no, hacemos un
        segundo llamado de Q&A plano para responder con los datos del viaje.
        """
        if fallback_reply and fallback_reply.lower() not in {
            "dale, contame más.",
            "dale contame más.",
            "dale, contame más",
            "perdón, no te entendí bien.",
        }:
            return fallback_reply
        try:
            return answer_question(update.message.text, _build_trip_context())
        except Exception:  # noqa: BLE001
            logger.exception("Q&A fallback inside intent failed")
            return fallback_reply or "Se me mezcló algo, tirame la pregunta de nuevo."

    if not action:
        final = await _reply_or_qa(reply)
        _record_turn(chat_id, "assistant", final)
        await update.message.reply_text(final)
        return

    pending, confirm_prompt = _build_pending_from_action(action)
    if not pending:
        final = await _reply_or_qa(reply)
        _record_turn(chat_id, "assistant", final)
        await update.message.reply_text(final)
        return

    _save_pending(chat_id, pending)
    full = reply.rstrip()
    if full and not full.endswith(("?", ".", "!")):
        full += "."
    full = f"{full}\n\n{confirm_prompt}" if full else confirm_prompt
    _record_turn(chat_id, "assistant", full)
    await update.message.reply_text(full, parse_mode="Markdown")


def _build_pending_from_action(action: dict) -> tuple[dict | None, str]:
    """Translate an LLM-suggested action into a pending_confirmations payload
    and a human confirmation prompt. Returns (None, "") if incomplete.
    """
    kind = action.get("type")
    if kind == "add_activity":
        day = action.get("day_number")
        description = (action.get("description") or "").strip()
        amount_eur = action.get("amount_eur")
        if not day or not description:
            return None, ""
        with get_db() as conn:
            row = conn.execute(
                "SELECT date, city FROM itinerary WHERE day_number = ?",
                (int(day),),
            ).fetchone()
        day_meta = f" ({row['date']} · {row['city']})" if row else ""
        price_part = f" · €{float(amount_eur):.2f}" if amount_eur else ""
        prompt = (
            f"¿Te lo anoto? 👉 *{description}* en el día {day}{day_meta}{price_part}\n"
            "Respondé *SI* para guardar o *NO* para dejarlo."
        )
        return (
            {
                "kind": "activity",
                "day_number": int(day),
                "description": description,
                "amount_eur": float(amount_eur) if amount_eur else None,
            },
            prompt,
        )

    if kind == "mark_paid":
        concept_hint = (action.get("checklist_concept") or "").strip()
        amount_eur = action.get("amount_eur")
        cid = _match_checklist(concept_hint, include_paid=False)
        if not cid:
            return None, ""
        with get_db() as conn:
            row = conn.execute(
                "SELECT concept FROM checklist WHERE id = ?", (cid,)
            ).fetchone()
        concept = row["concept"] if row else concept_hint
        price_part = f" con €{float(amount_eur):.2f}" if amount_eur else ""
        prompt = (
            f"¿Marco *{concept}* como pagado{price_part}?\n"
            "Respondé *SI* o *NO*."
        )
        return (
            {
                "kind": "mark_paid",
                "checklist_id": cid,
                "concept": concept,
                "amount_eur": float(amount_eur) if amount_eur else None,
            },
            prompt,
        )

    if kind == "update_checklist_amount":
        concept_hint = (action.get("checklist_concept") or "").strip()
        amount_eur = action.get("amount_eur")
        if amount_eur is None:
            return None, ""
        cid = _match_checklist(concept_hint, include_paid=True)
        if not cid:
            return None, ""
        with get_db() as conn:
            row = conn.execute(
                "SELECT concept, amount_eur, status FROM checklist WHERE id = ?",
                (cid,),
            ).fetchone()
        concept = row["concept"] if row else concept_hint
        old = (
            f" (estaba en €{row['amount_eur']:.2f})"
            if row and row["amount_eur"] is not None
            else ""
        )
        prompt = (
            f"¿Corrijo el importe de *{concept}* a €{float(amount_eur):.2f}{old}?\n"
            "*SI* / *NO*"
        )
        return (
            {
                "kind": "update_checklist_amount",
                "checklist_id": cid,
                "concept": concept,
                "amount_eur": float(amount_eur),
            },
            prompt,
        )

    if kind == "mark_paid_roundtrip":
        concept_ida = (action.get("checklist_concept_ida") or "").strip()
        concept_vuelta = (action.get("checklist_concept_vuelta") or "").strip()
        total_eur = action.get("total_eur") or action.get("amount_eur")
        if not concept_ida or not concept_vuelta or total_eur is None:
            return None, ""
        cid_ida = _match_checklist(concept_ida, include_paid=True)
        cid_vuelta = _match_checklist(concept_vuelta, include_paid=True)
        if not cid_ida or not cid_vuelta:
            return None, ""
        per_leg = round(float(total_eur) / 2, 2)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, concept FROM checklist WHERE id IN (?, ?)",
                (cid_ida, cid_vuelta),
            ).fetchall()
        names = {r["id"]: r["concept"] for r in rows}
        prompt = (
            f"Ida y vuelta €{float(total_eur):.2f} → €{per_leg:.2f} c/u.\n"
            f"¿Marco como pagado *{names.get(cid_ida, concept_ida)}* y "
            f"*{names.get(cid_vuelta, concept_vuelta)}* con ese importe? *SI* / *NO*"
        )
        return (
            {
                "kind": "mark_paid_roundtrip",
                "checklist_id_ida": cid_ida,
                "checklist_id_vuelta": cid_vuelta,
                "concept_ida": names.get(cid_ida, concept_ida),
                "concept_vuelta": names.get(cid_vuelta, concept_vuelta),
                "per_leg_eur": per_leg,
                "total_eur": float(total_eur),
            },
            prompt,
        )

    return None, ""


def _build_trip_context() -> str:
    """Return a compact text snapshot of the trip state for the LLM."""
    rate = float(get_config("eur_usd_rate", "1.172") or "1.172")
    start = get_config("start_date", "2026-07-25") or "2026-07-25"
    end = get_config("end_date", "2026-08-15") or "2026-08-15"
    traveler = get_config("traveler", "Felipe Veiga") or "Felipe Veiga"

    today = date.today()
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    total_days = (end_d - start_d).days + 1
    if today < start_d:
        trip_state = f"faltan {(start_d - today).days} días para el viaje"
    elif today > end_d:
        trip_state = "el viaje ya terminó"
    else:
        current_day = (today - start_d).days + 1
        trip_state = f"en viaje, día {current_day} de {total_days}"

    with get_db() as conn:
        itin = conn.execute(
            """SELECT day_number, date, city, activity, estimated_expense,
                      real_expense FROM itinerary ORDER BY day_number"""
        ).fetchall()
        ch = conn.execute(
            "SELECT id, concept, detail, amount_eur, status, reservation_code "
            "FROM checklist ORDER BY id"
        ).fetchall()

    est = sum((r["estimated_expense"] or 0) for r in itin)
    spent = sum((r["real_expense"] or 0) for r in itin)
    paid_items = [r for r in ch if r["status"] == "paid"]
    pending_items = [r for r in ch if r["status"] != "paid"]
    paid = sum((r["amount_eur"] or 0) for r in paid_items)
    pending_known = sum((r["amount_eur"] or 0) for r in pending_items if r["amount_eur"] is not None)
    pending_unknown_count = sum(1 for r in pending_items if r["amount_eur"] is None)
    total_known = est + paid + pending_known

    def eur(v: float) -> str:
        return f"€{v:,.2f}"

    lines = [
        f"Viajero: {traveler}",
        f"Fechas: {start} → {end} ({total_days} días, {trip_state})",
        f"Hoy: {today.isoformat()}",
        f"Tipo de cambio EUR/USD: {rate}",
        "",
        "Presupuesto (usá ESTOS números exactos en las respuestas):",
        f"  Pagado hasta ahora: {eur(paid)}  (suma de los items del checklist con status=paid)",
        f"  Pendiente con monto conocido: {eur(pending_known)}",
        f"  Pendientes SIN monto cargado: {pending_unknown_count} items "
        f"(faltan comprobantes/precios — NO los cuentes como €0, mencionalos como 'a definir')",
        f"  Gastado en el viaje (itinerario.real_expense): {eur(spent)}",
        f"  Estimado del itinerario (base comida + actividades): {eur(est)}",
        f"  Total estimado del viaje (con lo conocido): {eur(total_known)}",
        "",
        "Itinerario completo:",
    ]
    for r in itin:
        real = (
            f" · real €{r['real_expense']:.2f}"
            if r["real_expense"] is not None
            else ""
        )
        lines.append(
            f"  Día {r['day_number']} · {r['date']} · {r['city']} · "
            f"{r['activity']} · est €{r['estimated_expense']:.2f}{real}"
        )
    lines.append("")
    lines.append("Checklist de pagos:")
    for r in ch:
        mark = "✅ pagado" if r["status"] == "paid" else "⏳ pendiente"
        amount = f" · €{r['amount_eur']:.2f}" if r["amount_eur"] is not None else ""
        code = f" · cod {r['reservation_code']}" if r["reservation_code"] else ""
        detail = f" ({r['detail']})" if r["detail"] else ""
        lines.append(f"  [{r['id']}] {mark} · {r['concept']}{detail}{amount}{code}")
    return "\n".join(lines)


def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(MessageHandler(filters.PHOTO, handle_document_or_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_or_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    app = build_app()
    logger.info("Starting TripDesk bot (authorized chat_id=%s)", ALLOWED_CHAT_ID or "<unset>")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
