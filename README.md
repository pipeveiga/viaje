# TripDesk · Europa 2026

Guía central del viaje de Felipe Veiga por Europa (25 jul → 15 ago 2026).
Web app + bot de Telegram que procesa facturas y tickets con Claude AI.

## Features

- **Dashboard** con resumen financiero (presupuesto total, pagado, pendiente, gastado) en EUR y USD.
- **Itinerario** de 22 días editable, con gasto estimado vs. real y resaltado del día actual.
- **Checklist** de pagos anticipados (vuelos, hoteles, trenes, entradas, transporte).
- **Documentos**: subí facturas y reservas manualmente o automáticamente desde el bot.
- **Bot de Telegram** con comandos `/resumen`, `/hoy`, `/pendientes` y procesamiento automático de fotos y PDFs vía Claude API.

## Stack

- Backend: Python + FastAPI
- DB: SQLite (archivo local)
- Frontend: HTML + Tailwind CDN + vanilla JS (single page)
- Bot: `python-telegram-bot`
- IA: OpenAI (`gpt-4o-mini`) — visión para imágenes, extracción de texto con `pypdf` para PDFs

## Requisitos

- Python 3.11+
- Cuenta de OpenAI con API key
- Bot de Telegram creado con `@BotFather`
- Tu `chat_id` de Telegram (obtenelo con `@userinfobot` o similar)

## Instalación local

```bash
# 1. Clonar y entrar al repo
git clone <repo-url>
cd tripdesk

# 2. Copiar el .env
cp .env.example .env
# Editá .env y completá:
#   OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 3. Instalar dependencias
python -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 4. Inicializar la base de datos (crea tablas + carga itinerario y checklist)
python main.py --init-db

# 5. Correr la app (dos terminales)
# Terminal A — servidor web:
uvicorn main:app --reload
# → http://localhost:8000

# Terminal B — bot de Telegram:
python bot.py
```

## Estructura

```
tripdesk/
├── main.py              # FastAPI app + inicialización DB
├── bot.py               # Telegram bot (polling)
├── database.py          # SQLite (sqlite3 puro)
├── ai_processor.py      # OpenAI — extracción de documentos
├── routers/
│   ├── itinerary.py
│   ├── checklist.py
│   ├── documents.py
│   ├── config.py
│   └── summary.py
├── static/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── uploads/             # Archivos subidos (ignorado por git)
├── requirements.txt
├── .env.example
└── README.md
```

## API

| Método | Path | Descripción |
|---|---|---|
| `GET` | `/api/itinerary` | Itinerario completo |
| `GET` | `/api/itinerary/{day}` | Día específico |
| `PUT` | `/api/itinerary/{day}` | Actualizar gasto real / estado |
| `GET` | `/api/checklist` | Checklist de pagos |
| `PUT` | `/api/checklist/{id}` | Marcar como pagado/pendiente |
| `POST` | `/api/checklist/reset-paid` | Reset: todos los pagados → pendientes, limpia montos |
| `GET` | `/api/documents` | Listar documentos |
| `POST` | `/api/documents` | Subir documento (multipart) |
| `GET` | `/api/documents/{id}/file` | Descargar archivo |
| `DELETE` | `/api/documents/{id}` | Eliminar documento |
| `GET` | `/api/summary` | Resumen financiero + trip info |
| `GET/PUT` | `/api/config` | Tipo de cambio y metadata |

## Bot

Comandos:

- `/start` o `/help` — ayuda
- `/resumen` — resumen financiero actual
- `/hoy` — plan del día actual
- `/pendientes` — pagos pendientes

**Procesamiento automático:** mandá una foto o PDF de una factura, reserva o ticket. El bot lo manda a OpenAI (`gpt-4o-mini`), extrae la info en JSON y te muestra un resumen. Respondé `SI` para confirmar (se guarda en la DB, se marca el checklist correspondiente si matchea, y se suma al día del itinerario) o `NO` para cancelar.

El bot **solo responde al `TELEGRAM_CHAT_ID` configurado en `.env`**, los demás mensajes se descartan.

## Deploy en Railway

1. Creá una cuenta en [railway.app](https://railway.app) y conectá tu repo GitHub.
2. New Project → Deploy from GitHub repo → seleccioná el repo.
3. En **Variables**, agregá todas las del `.env.example` completadas.
4. Railway detecta Python. Configurá dos servicios apuntando al mismo repo:
   - **Servicio web**: start command `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Servicio bot**: start command `python bot.py`
5. En el servicio web, generá un dominio público (`Settings → Generate Domain`).
6. (Opcional) Configurá un volumen persistente para `tripdesk.db` y `uploads/`.

## Deploy en Render

Similar a Railway: Web Service para FastAPI y Background Worker para el bot, compartiendo el mismo repo y variables de entorno.

## Webhook de Telegram (opcional)

El bot corre por defecto en modo polling, así que no necesitás webhook. Si querés usar webhook en vez de polling:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<tu-dominio>/api/telegram/webhook"
```

Y reemplazá `app.run_polling()` por el handler webhook correspondiente (ver docs de `python-telegram-bot`).

## Notas

- Todos los montos se almacenan en EUR. La conversión a USD es al vuelo con el tipo de cambio editable desde el dashboard (⚙️).
- Los archivos subidos se guardan en `/uploads` con nombre UUID para evitar colisiones.
- El dashboard hace polling cada 30 s; no hay websockets.
- El itinerario funciona offline una vez cargados los datos (los lee de la DB local).
