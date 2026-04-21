"""Claude API wrapper: extract structured data from a travel document.

Accepts image bytes (JPG/PNG) or PDF bytes and returns a dict matching the
schema requested in the system prompt.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from anthropic import Anthropic

MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """Sos un asistente de viajes. El usuario te manda una factura, ticket,
reserva o comprobante de pago. Extraé la siguiente información en JSON:
{
  "tipo": "vuelo"|"hotel"|"transporte"|"museo_entrada"|"comida"|"otro",
  "descripcion": string,
  "monto_eur": number | null,
  "monto_usd": number | null,
  "fecha": string (YYYY-MM-DD) | null,
  "proveedor": string | null,
  "numero_reserva": string | null,
  "dia_viaje": number | null (del 1 al 22),
  "coincide_checklist": string | null,
  "confianza": "alta" | "media" | "baja"
}
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
    """Try to parse JSON from the model reply, tolerating stray text."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"Could not parse JSON from model response: {text[:200]}")


def process_document(
    file_bytes: bytes,
    filename: str,
    checklist_items: list[dict] | None = None,
    itinerary_days: list[dict] | None = None,
) -> dict[str, Any]:
    """Send the document to Claude and return the extracted dict.

    `checklist_items` and `itinerary_days` are included in the user message as
    context so the model can suggest which checklist entry or trip day the
    document belongs to.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = Anthropic(api_key=api_key)

    is_pdf = filename.lower().endswith(".pdf")
    b64 = base64.standard_b64encode(file_bytes).decode("ascii")

    if is_pdf:
        source = {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        }
        doc_block = {"type": "document", "source": source}
    else:
        source = {
            "type": "base64",
            "media_type": _media_type_for(filename),
            "data": b64,
        }
        doc_block = {"type": "image", "source": source}

    context_lines = []
    if checklist_items:
        context_lines.append("Items del checklist pendientes/pagados:")
        for item in checklist_items:
            context_lines.append(
                f"- id={item['id']} · {item['concept']} ({item.get('status', '?')})"
            )
    if itinerary_days:
        context_lines.append("\nDías del viaje:")
        for d in itinerary_days:
            context_lines.append(
                f"- día {d['day_number']} · {d['date']} · {d['city']} · {d['activity']}"
            )
    context_text = "\n".join(context_lines) if context_lines else "Sin contexto adicional."

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    doc_block,
                    {
                        "type": "text",
                        "text": (
                            "Contexto del viaje (para sugerir coincidencias):\n"
                            + context_text
                        ),
                    },
                ],
            }
        ],
    )

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    return _extract_json(text)
