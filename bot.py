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

from ai_processor import answer_question, process_document
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
    "✈️ *TripDesk Bot*\n\n"
    "Comandos rápidos:\n"
    "• /resumen — resumen financiero del viaje\n"
    "• /hoy — plan del día actual\n"
    "• /pendientes — pagos pendientes del checklist\n\n"
    "📎 Mandame fotos o PDFs de facturas, reservas o tickets y los proceso"
    " automáticamente (te pido SI/NO antes de guardar).\n\n"
    "💬 Y escribime cualquier pregunta sobre el viaje (ej: _\"cuánto me queda"
    " por pagar?\"_, _\"qué hago el 4-ago?\"_, _\"en qué hotel me quedo en"
    " Roma?\"_) y te contesto con la info del itinerario."
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
    text = (
        f"📅 *Día {row['day_number']}* — {row['date']}\n"
        f"📍 {row['city']}\n"
        f"🎯 {row['activity']}\n"
        f"💶 Estimado: €{row['estimated_expense']:.2f}"
    )
    if row["real_expense"] is not None:
        text += f"\n💰 Real: €{row['real_expense']:.2f}"
    await update.message.reply_text(text, parse_mode="Markdown")


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
    hint = name_hint.lower()
    for r in rows:
        if r["concept"].lower() in hint or hint in r["concept"].lower():
            return r["id"]
    return None


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
            """SELECT id, description, amount_eur, checklist_id
               FROM documents
               WHERE confirmed = 1 AND reservation_number = ?
               ORDER BY id DESC LIMIT 1""",
            (reservation_number,),
        ).fetchone()
    return dict(row) if row else None


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
    checklist_lines = []
    if is_duplicate:
        dup_desc = dup["description"] if dup else None
        checklist_lines.append(
            f"🔁 Duplicado de documento previo"
            + (f" ({dup_desc})" if dup_desc else "")
        )
        if checklist_id:
            name = concept_by_id.get(checklist_id)
            if name:
                checklist_lines.append(
                    f"📝 Se actualizará el monto de: {name}"
                )
        if checklist_id_vuelta:
            name = concept_by_id.get(checklist_id_vuelta)
            if name:
                checklist_lines.append(
                    f"📝 Se actualizará el monto de: {name}"
                )
    else:
        if checklist_id:
            name = concept_by_id.get(checklist_id)
            if name:
                checklist_lines.append(f"✅ Se marcará como pagado: {name}")
        if checklist_id_vuelta:
            name = concept_by_id.get(checklist_id_vuelta)
            if name:
                checklist_lines.append(f"✅ Se marcará como pagado: {name}")
    checklist_block = ("\n" + "\n".join(checklist_lines)) if checklist_lines else ""

    day_line = ""
    if payload["day_number"] and not is_roundtrip and not is_duplicate and not checklist_id:
        day_line = f"\n📅 Se agregará al día {payload['day_number']} del itinerario"

    roundtrip_tag = " (ida y vuelta)" if is_roundtrip else ""
    dup_tag = " [duplicado]" if is_duplicate else ""

    summary = (
        f"🔍 *Detecté:*\n"
        f"📄 {payload['description'] or 'documento'}{roundtrip_tag}{dup_tag}\n"
        f"📆 Fecha: {payload['date'] or 's/d'}\n"
        f"💶 Monto: {amount_str}\n"
        f"🏷️ Tipo: {payload['doc_type'] or 'otro'}"
        f"{checklist_block}{day_line}\n\n"
        "¿Es correcto? Respondé *SI* para confirmar o *NO* para cancelar."
    )
    await msg.reply_text(summary, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    text = (update.message.text or "").strip().lower()

    if text in {"si", "sí", "s", "yes", "y"}:
        data = _consume_pending(str(update.message.chat_id))
        if not data:
            await update.message.reply_text("No hay nada pendiente de confirmar.")
            return

        today_iso = datetime.now().date().isoformat()
        per_leg_eur = data.get("amount_eur")
        per_leg_usd = data.get("amount_usd")
        total_eur = data.get("amount_total_eur")
        total_usd = data.get("amount_total_usd")
        is_roundtrip = bool(data.get("is_roundtrip"))
        is_duplicate = bool(data.get("is_duplicate"))

        doc_eur = total_eur if is_roundtrip else per_leg_eur
        doc_usd = total_usd if is_roundtrip else per_leg_usd

        with get_db() as conn:
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

            for cid in (data.get("checklist_id"), data.get("checklist_id_vuelta")):
                if not cid:
                    continue
                if is_duplicate:
                    # El doc original ya marcó el item como pagado. Si el nuevo
                    # doc tiene un monto (factura real), actualizamos el importe
                    # del checklist; no cambiamos status ni paid_date.
                    if per_leg_eur is not None:
                        conn.execute(
                            "UPDATE checklist SET amount_eur = ? WHERE id = ?",
                            (per_leg_eur, cid),
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

            # Gastos del día: solo si el doc NO es un pago anticipado (no matchea
            # checklist), no es ida y vuelta y no es un duplicado.
            should_touch_itinerary = (
                not is_roundtrip
                and not is_duplicate
                and not data.get("checklist_id")
                and data.get("day_number")
                and per_leg_eur is not None
            )
            if should_touch_itinerary:
                row = conn.execute(
                    "SELECT real_expense FROM itinerary WHERE day_number = ?",
                    (data["day_number"],),
                ).fetchone()
                current = (row["real_expense"] or 0) if row else 0
                new_value = current + float(per_leg_eur)
                conn.execute(
                    """UPDATE itinerary
                       SET real_expense = ?, status = 'completed'
                       WHERE day_number = ?""",
                    (new_value, data["day_number"]),
                )

        if is_duplicate:
            await update.message.reply_text(
                "✅ Guardado como respaldo. No volví a marcar el pago (ya estaba confirmado)."
            )
        else:
            await update.message.reply_text(
                "✅ Guardado. Todo actualizado en la app."
            )
        return

    if text in {"no", "n", "cancel", "cancelar"}:
        consumed = _consume_pending(str(update.message.chat_id))
        if consumed:
            try:
                Path(consumed["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            await update.message.reply_text("❌ Cancelado. No se guardó nada.")
        else:
            await update.message.reply_text("No hay nada pendiente de confirmar.")
        return

    # Cualquier otro texto → Q&A con OpenAI usando el contexto del viaje.
    await _answer_free_text(update, context)


async def _answer_free_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await context.bot.send_chat_action(
        chat_id=update.message.chat_id, action=ChatAction.TYPING
    )
    try:
        context_block = _build_trip_context()
        answer = answer_question(update.message.text, context_block)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Q&A failed")
        await update.message.reply_text(f"⚠️ No pude contestar: {exc}")
        return
    await update.message.reply_text(answer)


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
    paid = sum((r["amount_eur"] or 0) for r in ch if r["status"] == "paid")
    pending = sum((r["amount_eur"] or 0) for r in ch if r["status"] == "pending")
    total = est + paid + pending

    def eur(v: float) -> str:
        return f"€{v:,.2f}"

    lines = [
        f"Viajero: {traveler}",
        f"Fechas: {start} → {end} ({total_days} días, {trip_state})",
        f"Hoy: {today.isoformat()}",
        f"Tipo de cambio EUR/USD: {rate}",
        "",
        "Presupuesto:",
        f"  Total estimado: {eur(total)}",
        f"  Pagado: {eur(paid)}",
        f"  Pendiente por pagar: {eur(pending)}",
        f"  Gastado en viaje: {eur(spent)}",
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
