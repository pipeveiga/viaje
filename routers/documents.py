"""Document upload and listing endpoints."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import get_db

router = APIRouter(prefix="/api/documents", tags=["documents"])

UPLOADS_PATH = os.getenv("UPLOADS_PATH", "uploads/")


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "file_path": row["file_path"],
        "doc_type": row["doc_type"],
        "description": row["description"],
        "amount_eur": row["amount_eur"],
        "amount_usd": row["amount_usd"],
        "date": row["date"],
        "provider": row["provider"],
        "reservation_number": row["reservation_number"],
        "day_number": row["day_number"],
        "checklist_id": row["checklist_id"],
        "confidence": row["confidence"],
        "confirmed": bool(row["confirmed"]),
        "created_at": row["created_at"],
    }


@router.get("")
def list_documents():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


@router.post("")
async def upload_document(
    file: UploadFile = File(...),
    doc_type: str = Form("otro"),
    description: str = Form(""),
    amount_eur: float | None = Form(None),
    date: str | None = Form(None),
    provider: str | None = Form(None),
    reservation_number: str | None = Form(None),
    day_number: int | None = Form(None),
):
    Path(UPLOADS_PATH).mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix if file.filename else ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = Path(UPLOADS_PATH) / unique_name

    content = await file.read()
    dest.write_bytes(content)

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO documents
               (filename, file_path, doc_type, description, amount_eur,
                date, provider, reservation_number, day_number, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                file.filename or unique_name,
                str(dest),
                doc_type,
                description,
                amount_eur,
                date,
                provider,
                reservation_number,
                day_number,
            ),
        )
        doc_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return _row_to_dict(row)


@router.get("/{doc_id}/file")
def download_document(doc_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="File missing on disk")
        return FileResponse(path, filename=row["filename"])


@router.delete("/{doc_id}")
def delete_document(doc_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return {"ok": True}
