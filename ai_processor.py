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
