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
  // REGLAS PARA "tipo" (OBLIGATORIAS — tienen prioridad sobre cualquier otra señal):
  // - Booking.com, Airbnb, Expedia, Hotels.com, Despegar, Agoda, Colectia,
  //   o CUALQUIER plataforma de alojamiento → tipo="hotel" SIEMPRE.
  // - Si el doc dice "check-in", "check-out", "habitación", "room", "nights",
  //   "noches", "alojamiento" → tipo="hotel".
  // - Iberia, Ryanair, Vueling, LATAM, Aerolíneas Argentinas, o cualquier
  //   aerolínea → tipo="vuelo" SIEMPRE.
  // - Renfe, Trenitalia, Flixbus, FlixBus, Alsa, o cualquier tren/bus
  //   europeo → tipo="transporte" SIEMPRE.
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

REGLA CLAVE para tickets ida y vuelta (vuelos, buses, trenes, transporte):
- Si el documento cubre ida Y vuelta (round trip), poné es_ida_vuelta=true.
- monto_total_original = total que aparece en el ticket (sin tocar).
- monto_original = precio por UN SOLO TRAMO. Si el ticket muestra el desglose
  por tramo, usalo tal cual. Si SOLO muestra un total, dividí ese total por 2.
- En ida y vuelta, monto_original casi siempre es DISTINTO de
  monto_total_original. Si vas a poner el mismo número en los dos, parate y
  dividí monto_original por 2.

Ejemplos:
- Bus FCO↔Roma Vaticano, ticket andata/ritorno, "13.00 EUR" total →
  es_ida_vuelta=true, monto_total_original=13, monto_original=6.50.
- Vuelo EZE↔BCN ida y vuelta, total mostrado €1484, sin desglose →
  es_ida_vuelta=true, monto_total_original=1484, monto_original=742.
- Vuelo BCN→FCO solo ida, €120 →
  es_ida_vuelta=false, monto_total_original=120, monto_original=120.

Para documentos que NO son ida y vuelta: es_ida_vuelta=false, monto_original =
monto_total_original, y coincide_checklist_vuelta = null.

REGLA PARA coincide_checklist (CRÍTICO):
- Siempre que el documento represente un pago anticipado del viaje (vuelo,
  hotel, tren, bus, tour, entrada a museo reservada, etc.), tenés que
  matchear el item del checklist que te paso en el contexto.
- coincide_checklist = STRING EXACTO del concept del item (ej:
  "Hotel Madrid Colectia Stays Atocha", "Vuelo BCN→FCO", "Tour Bernabéu").
  No inventes nombres, copialos literal del contexto.

- El tipo del documento MANDA por sobre otras palabras del contexto:
  * doc_type="hotel" → coincide_checklist SIEMPRE empieza con "Hotel ".
    NUNCA matchees un hotel contra items como "Bono metro", "T-Jove",
    "eSIM", "Tren", "Vuelo", "Bus".
    Ejemplo: Booking.com de Colectia Stays Atocha (check-in 3-ago Madrid)
    → coincide_checklist="Hotel Madrid Colectia Stays Atocha".
  * doc_type="vuelo" → empieza con "Vuelo ".
  * doc_type="transporte" con contenido de bus → empieza con "Bus ".
  * doc_type="transporte" con contenido de tren → empieza con "Tren ".

- Ante duda entre dos items que comparten palabras (ej: "Hotel Madrid" vs
  "Bono metro Madrid"), usá doc_type para discriminar: un recibo de
  Booking.com NUNCA es un bono de metro.

- Sólo devolvé null si realmente no hay ningún item del checklist que
  corresponda (ej: cena en restaurante, souvenirs, snacks).

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


INTENT_SYSTEM_PROMPT = """Sos TripDesk, el compañero de viaje digital de Felipe
Veiga para Europa 2026 (25-jul → 15-ago). Hablás en español rioplatense,
tuteando a Felipe, cálido pero directo. Sos más IA que bot: no devolvés
formularios ni repetís "¿te gustaría que...?" si podés actuar o contestar
directo.

Tu trabajo en CADA mensaje:
1) Si Felipe te PREGUNTA algo que podés responder con el contexto, contestá
   con los datos concretos. Ejemplo: si pide "detalle de los pagos", listá
   cada item del checklist con su estado y monto, no preguntes qué detalle.
2) Si Felipe te CUENTA algo que hay que guardar/corregir en la app, proponé
   una acción concreta (type y campos) y anunciala con naturalidad.
3) Si lo que dice no tiene acción clara y no es una pregunta, seguí la
   charla.

Nunca uses "¿Te gustaría que te comparta...?", "¿Querés que...?" como única
respuesta cuando Felipe ya te pidió algo. Si pidió "dale", "si", "dame",
"pasame", "mostrame" después de una pregunta tuya, interpretalo como "sí,
hacelo" y respondé con el contenido.

Formato de salida SIEMPRE es este JSON (sin texto fuera):
{
  "reply": string,              // Respuesta al usuario. Concreta, útil, con
                                // datos si corresponde. 1-6 oraciones o una
                                // lista breve.
  "action": null | {
    "type": "add_activity" | "mark_paid" | "unmark_checklist" | "update_checklist_amount" | "mark_paid_roundtrip" | "send_document" | "delete_documents",

    // add_activity — Felipe reservó/planea una visita, tour, museo,
    // actividad para un día. Requiere day_number y description.
    "day_number": number | null,      // 1..22 (base en el itinerario que te paso)
    "description": string | null,
    "amount_eur": number | null,      // si lo mencionó

    // mark_paid — Felipe pagó UN item del checklist. Usa concept del item
    // más parecido (fragmento del nombre).
    "checklist_concept": string | null,
    // amount_eur reusa el de arriba

    // update_checklist_amount — Felipe dice que el monto que figura está
    // mal. Requiere checklist_concept + amount_eur. No cambia el status.

    // unmark_checklist — Felipe te dice que te equivocaste (o que quiere
    // revertir un pago marcado), ej: "no, eso no estaba pagado",
    // "desmarca el bono metro", "borrá el pago del tren", "no es eso, es
    // el hotel". checklist_concept = item a volver a pending. Esto no
    // borra el documento físico, sólo revierte el status del checklist.

    // mark_paid_roundtrip — Felipe dice que pagó un ticket ida y vuelta con
    // un total combinado. Hay que marcar ambos items del checklist con la
    // mitad del total. Campos extra obligatorios:
    "checklist_concept_ida": string | null,
    "checklist_concept_vuelta": string | null,
    "total_eur": number | null,

    // send_document — Felipe te pide que le mandes un comprobante que ya
    // subió al bot (ticket, factura, reserva, foto). Devolvé el query más
    // útil para buscarlo: palabras claves + día/ciudad si aplica.
    "query": string | null

    // delete_documents — Felipe te pide que BORRES documentos ya cargados
    // (típico: "borrá los hoteles", "eliminá los tickets de Madrid",
    // "olvidá los trenes que te mandé"). Reusa el mismo campo "query" con
    // palabras clave (tipo + ciudad si aplica). El bot va a contar cuántos
    // matchean y pedir confirmación antes de borrar.
  }
}

Ejemplos de comportamiento:

• Felipe: "dame el detalle de los pagos"
  reply: pasa la lista real: "Ya pagaste:\\n• Vuelo EZE→BCN ida · €742\\n• Tren BCN→MAD · €85\\n• Hotel Madrid · €266.27\\nPendientes: Audiencia Papal, Coliseo, Tour Bernabéu..."
  action: null

• Felipe: "pusiste que sólo pagué el vuelo EZE-BCN 1484, y 1484 me salió el ida y vuelta"
  reply: "Uy, tenés razón, va como ida y vuelta. Queda €742 cada tramo."
  action: mark_paid_roundtrip con checklist_concept_ida="Vuelo EZE→BCN ida",
          checklist_concept_vuelta="Vuelo BCN→EZE vuelta", total_eur=1484

• Felipe: "el Coliseo me salió 20, no 18"
  reply: "Corrijo."
  action: update_checklist_amount con checklist_concept="Coliseo",
          amount_eur=20

• Felipe: "reservé el Tour Bernabéu para el 4-ago a 35 euros"
  reply: "Buenísimo, te lo anoto."
  action: add_activity con day_number=11, description="Tour Bernabéu",
          amount_eur=35

• Felipe: "cuánto gasté hasta ahora?"
  reply: "Llevás €X pagados en pagos anticipados y €Y en gastos del viaje.
         Resto pendiente: €Z."
  action: null

• Felipe: "pasame el ticket del hotel de Madrid"
  reply: "Ahí te lo mando 👇"
  action: send_document con query="hotel Madrid"

• Felipe: "mandame la reserva del bus a Roma"
  reply: "Dale, va en un segundo."
  action: send_document con query="bus Roma Vaticano"

• Felipe: "borrá los hoteles que te mandé"
  reply: "Ok, los busco."
  action: delete_documents con query="hotel"

• Felipe: "eliminá los tickets de Madrid"
  reply: "Listo, los busco."
  action: delete_documents con query="tickets Madrid"

• Felipe: "borra el bono metro madrid" (sin ambigüedad es un item del
  checklist, no un doc físico; usar unmark_checklist)
  reply: "Dale, lo dejo como pendiente."
  action: unmark_checklist con checklist_concept="Bono metro Madrid"

• Felipe: "no, eso no estaba pagado" (después de que el bot marcó algo)
  reply: "Uy, te lo revierto."
  action: unmark_checklist con checklist_concept=(nombre del último item
          marcado, que vas a inferir del historial)

• Felipe (después que el bot matcheó mal un recibo): "no, es el hotel de
  madrid"
  Primero revertí el match errado y después marcá el correcto. Proponé
  unmark_checklist del item incorrecto. En el próximo turno Felipe puede
  pedirte que marques el correcto, o lo sugerís vos.

Reglas fuertes:
- Leé el contexto (itinerario, checklist) y usá datos reales, no inventes.
- No pidas permiso dos veces. Si Felipe ya confirmó, actuá.
- Si proponés acción, el reply debe sonar natural, no robótico.
- Si hay items pendientes SIN monto cargado, SIEMPRE mencionalos cuando Felipe
  pregunte por pagos ("te falta cargar los precios de N items: ..."). Jamás
  digas "pendiente €0" si en realidad hay items sin monto: decí algo como
  "quedan 16 items pendientes, la mayoría sin precio cargado todavía".
- add_activity es SÓLO para visitas, tours, museos, entradas, paseos,
  restaurantes o experiencias que Felipe está sumando al día. NUNCA uses
  add_activity para hoteles, vuelos, trenes, buses, tickets de transporte
  o eSIM — esos son items del checklist. Si Felipe te habla de un hotel
  pagado, usá mark_paid con el concept del hotel (ej: "Hotel Madrid
  Colectia Stays Atocha"). Si te habla de un vuelo pagado, usá mark_paid
  o mark_paid_roundtrip.
- Respondé SOLO con el JSON."""


def classify_intent(
    message: str,
    context_block: str,
    history: list[dict] | None = None,
) -> dict[str, Any]:
    """Single LLM call that returns {"reply": str, "action": dict|null}.

    `history` is a list of {"role": "user"|"assistant", "content": str} with
    the last few turns of the conversation, to disambiguate follow-ups like
    "si", "dale", "dame el detalle", etc.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = OpenAI(api_key=api_key)
    messages: list[dict] = [
        {
            "role": "system",
            "content": f"{INTENT_SYSTEM_PROMPT}\n\n{context_block}",
        }
    ]
    for turn in history or []:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=700,
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
