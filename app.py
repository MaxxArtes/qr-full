# app.py
import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dateutil.tz import tzlocal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from qr_utils import decode_qr_bytes

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

with engine.begin() as conn:
    conn.execute(text("""
    CREATE TABLE IF NOT EXISTS scans (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
        source TEXT,
        data_raw TEXT NOT NULL
    );
    """))

app = FastAPI(title="QR Full (FastAPI + PWA)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

def now_iso() -> str:
    return datetime.now(tzlocal()).isoformat()

class QRText(BaseModel):
    text: str
    source: Optional[str] = "pwa"

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse("static/manifest.webmanifest")

@app.get("/sw.js")
def sw():
    return FileResponse("static/sw.js", media_type="text/javascript")

@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/save_text")
def save_text(payload: QRText):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO scans (source, data_raw) VALUES (:s, :d)"),
            {"s": payload.source or "pwa", "d": payload.text}
        )
    return {"ok": True}

@app.post("/scan")
async def scan_image(file: UploadFile = File(...), source: Optional[str] = Query(default="upload")):
    if not file.content_type or "image" not in file.content_type:
        raise HTTPException(status_code=400, detail="Envie uma imagem (content-type image/*).")
    data = await file.read()
    texts = decode_qr_bytes(data)
    if not texts:
        return {"found": 0, "items": []}
    with engine.begin() as conn:
        for t in texts:
            conn.execute(text("INSERT INTO scans (source, data_raw) VALUES (:s, :d)"),
                         {"s": source or (file.filename or "upload"), "d": t})
    return {"found": len(texts), "items": texts}

@app.get("/list")
def list_scans(limit: int = 100, q: Optional[str] = None):
    sql = "SELECT id, timestamp, source, data_raw FROM scans"
    params = {}
    if q:
        sql += " WHERE data_raw ILIKE :q"
        params["q"] = f"%{q}%"
    sql += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = limit
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return {
        "count": len(rows),
        "items": [{
            "id": r["id"],
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            "source": r["source"],
            "data_raw": r["data_raw"],
        } for r in rows]
    }

@app.get("/download")
def download_csv():
    def row_iter():
        yield ("id;timestamp;source;data_raw\n").encode("utf-8")
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text("SELECT id, timestamp, source, data_raw FROM scans ORDER BY id ASC")
            )
            for r in result:
                line = [
                    str(r.id),
                    r.timestamp.isoformat() if r.timestamp else "",
                    r.source or "",
                    (r.data_raw or "").replace("\\n", " ").replace("\\r", " ")
                ]
                yield (";".join(line) + "\\n").encode("utf-8")
    return StreamingResponse(row_iter(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="scans.csv"'})