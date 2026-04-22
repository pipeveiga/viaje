"""OpenAI wrapper: extract structured data from a travel document.

Accepts image bytes (JPG/PNG/WEBP) or PDF bytes and returns a dict matching
the schema in the system prompt. Uses gpt-4o-mini (cheap + supports vision).

For PDFs we extract text with pypdf first since the Chat Completions API
does not accept PDFs directly.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Any

from openai import OpenAI

MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """Sos un asistente de viajes. El usuario te manda una factura, ticket,
reserva o comprobante de pago. Extraé la siguiente información en JSON:
{
  "tipo": "vuelo"|"hotel"|"transporte"|"museo_entrada"|"comida"|"otro",
  "descripcion": string,
  "moneda": "EUR"|"USD"|"ARS"|"otro",      // moneda que aparece en el documento
  "monto_total_original": number | null,   // total del documento en su moneda original, tal como aparece
  "monto_original": number | null,         // monto a aplicar a cada item del checklist, en la moneda original (ver regla de ida y vuelta)
  "fecha": string (YYYY-MM-DD) | null,
  "proveedor": string | null,
  "numero_reserva": string | null,
  "dia_viaje": number | null (del 1 al 22),
  "es_ida_vuelta": boolean,                // true SOLO si es un ticket de vuelo ida y vuelta (round trip)
  "coincide_checklist": string | null,     // item de ida o item único
  "coincide_checklist_vuelta": string | null, // SOLO si es_ida_vuelta=true: item del tramo de vuelta
  "confianza": "alta" | "media" | "baja"
}

MONEDA:
- Identificá la moneda real del documento: EUR (€), USD (US$/USD), ARS (AR$/$ en Argentina).
- No conviertas nada: los montos van en su moneda original y el sistema los convierte después.

REGLA CLAVE para vuelos ida y vuelta:
- Si el documento cubre ida Y vuelta (round trip), poné es_ida_vuelta=true.
- En monto_total_original poné el total tal como sale en el ticket.
- En monto_original poné el precio por tramo: si el desglose está en el ticket usalo;
  si solo hay un total, dividilo por 2.
- En coincide_checklist sugerí el item de ida y en coincide_checklist_vuelta el
  item de vuelta (ej: "Vuelo EZE→BCN ida" y "Vuelo BCN→EZE vuelta").

Para documentos que NO son ida y vuelta: es_ida_vuelta=false, monto_original =
monto_total_original, y coincide_checklist_vuelta = null.

Respondé SOLO el JSON, sin texto extra."""


def _media_type_for(filename: str) -> str:
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "image/jpeg")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"Could not parse JSON from model response: {text[:200]}")


def _extract_pdf_text(file_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(file_bytes))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(parts).strip()


def _build_context(
    checklist_items: list[dict] | None,
    itinerary_days: list[dict] | None,
) -> str:
    lines = []
    if checklist_items:
        lines.append("Items del checklist (pendientes/pagados):")
        for item in checklist_items:
            lines.append(
                f"- id={item['id']} · {item['concept']} ({item.get('status', '?')})"
            )
    if itinerary_days:
        lines.append("\nDías del viaje:")
        for d in itinerary_days:
            lines.append(
                f"- día {d['day_number']} · {d['date']} · {d['city']} · {d['activity']}"
            )
    return "\n".join(lines) if lines else "Sin contexto adicional."


def process_document(
    file_bytes: bytes,
    filename: str,
    checklist_items: list[dict] | None = None,
    itinerary_days: list[dict] | None = None,
) -> dict[str, Any]:
    """Send the document to OpenAI and return the extracted dict."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)

    context_text = _build_context(checklist_items, itinerary_days)
    context_msg = f"Contexto del viaje (para sugerir coincidencias):\n{context_text}"

    is_pdf = filename.lower().endswith(".pdf")

    if is_pdf:
        pdf_text = _extract_pdf_text(file_bytes)
        if not pdf_text:
            raise ValueError(
                "No pude extraer texto del PDF (probablemente es un escaneo sin OCR)."
                " Mandame una foto/captura en su lugar."
            )
        user_content = [
            {
                "type": "text",
                "text": f"{context_msg}\n\nContenido del documento:\n{pdf_text}",
            }
        ]
    else:
        b64 = base64.standard_b64encode(file_bytes).decode("ascii")
        mime = _media_type_for(filename)
        user_content = [
            {"type": "text", "text": context_msg},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            },
        ]

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=1024,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    return _extract_json(text)


ASSISTANT_SYSTEM_PROMPT = """Sos TripDesk, el compañero de viaje digital de Felipe
Veiga. Tu tono es cercano, alentador y charlón (pero sin sobrar): le hablás de
vos, en español rioplatense, como un amigo que lo está ayudando a armar el
viaje a Europa en julio-agosto 2026. Usás emojis con moderación (1-2 por
mensaje). Respondés en 1 a 4 oraciones, o un poco más si hace falta dar varios
datos juntos. Si te pregunta algo concreto (plata, días, reservas) usás el
contexto que te paso y no inventás. Si no sabés, lo decís tranquilo y le
ofrecés lo que sí podés averiguar. Siempre usás EUR como moneda principal,
pero si le sirve mencionás también USD."""


def answer_question(question: str, context_block: str) -> str:
    """Run a stateless Q&A turn with OpenAI, injecting the trip context."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": f"{ASSISTANT_SYSTEM_PROMPT}\n\n{context_block}",
            },
            {"role": "user", "content": question},
        ],
        max_tokens=600,
    )
    return (resp.choices[0].message.content or "").strip()


INTENT_SYSTEM_PROMPT = """Sos TripDesk, el compañero de viaje de Felipe (Europa
2026). Cada mensaje que te manda, tenés que (a) contestarle con onda en
español rioplatense, y (b) decidir si te está contando algo para anotar en el
viaje. Si detectás algo anotable, proponés una acción concreta; si no, sólo
charlás o contestás la pregunta.

Devolvés SIEMPRE un JSON con este formato exacto:
{
  "reply": string,   // tu respuesta conversacional, breve y cálida
  "action": null | {
    "type": "add_activity" | "mark_paid",
    "day_number": number | null,       // 1..22 (para add_activity)
    "description": string | null,      // descripción corta (add_activity)
    "amount_eur": number | null,       // monto en EUR si Felipe lo mencionó
    "checklist_concept": string | null // parte del nombre del item del checklist (mark_paid)
  }
}

Tipos de acción:
- add_activity: Felipe te cuenta que planea/reservó una visita, museo, tour,
  actividad, comida especial, excursión, etc. para un día concreto. Si
  menciona la fecha (ej: "30-jul", "4 de agosto") deducí el day_number con el
  itinerario que te paso. Si no hay día claro, devolvé action=null y pedíselo
  en el reply.
- mark_paid: Felipe te dice que ya pagó algo que está en el checklist
  (vuelos, hoteles, trenes, tours). checklist_concept = fragmento del nombre
  del item para matchear (ej: "Hotel Roma" o "Tour Bernabéu").

Reglas:
- Si Felipe sólo hace una pregunta o charla general, action=null.
- No inventes acciones si hay dudas: es mejor preguntar en el reply.
- Tu reply nunca es vacío; siempre decí algo.
- Respondé SOLO con el JSON, sin texto fuera."""


def classify_intent(message: str, context_block: str) -> dict[str, Any]:
    """Single LLM call that returns {"reply": str, "action": dict|null}.

    The bot uses `action` (if present) as a pending confirmation (SI/NO),
    and always shows `reply` to the user.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": f"{INTENT_SYSTEM_PROMPT}\n\n{context_block}",
            },
            {"role": "user", "content": message},
        ],
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    try:
        data = _extract_json(text)
    except ValueError:
        return {"reply": text.strip() or "Perdón, no te entendí bien.", "action": None}
    if "reply" not in data or not data["reply"]:
        data["reply"] = "Dale, contame más."
    if "action" not in data:
        data["action"] = None
    return data
