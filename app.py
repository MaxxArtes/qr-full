# app.py  (API FastAPI + PWA + NFC-e: itens + CNPJ/loja/data)
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Tuple, Dict, Any

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sqlalchemy import (
    create_engine, text, Column, Integer, Text, DateTime, func, Numeric
)
from sqlalchemy.orm import sessionmaker, declarative_base

from qr_utils import decode_qr_bytes

# ---------------- Config ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# ---------------- ORM / Tabelas ----------------
Base = declarative_base()

class Scan(Base):
    __tablename__ = "scans"
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source = Column(Text)
    data_raw = Column(Text, nullable=False)
    # novos metadados NFC-e
    cnpj = Column(Text)
    store_name = Column(Text)
    purchase_date = Column(DateTime(timezone=True))

class ScanItem(Base):
    __tablename__ = "scan_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(Integer, nullable=False)
    name = Column(Text, nullable=False)
    qty = Column(Numeric(14, 4))
    unit_price = Column(Numeric(14, 4))
    total_price = Column(Numeric(14, 2))

Base.metadata.create_all(engine)

# migração leve para adicionar colunas se faltarem (SQLite e Postgres)
def ensure_scan_extra_columns():
    with engine.begin() as conn:
        dialect = engine.dialect.name
        needed = {"cnpj": "TEXT", "store_name": "TEXT", "purchase_date": "TIMESTAMP"}
        if dialect == "sqlite":
            rows = conn.execute(text("PRAGMA table_info(scans)")).fetchall()
            existing = {row[1] for row in rows}
            for col, typ in needed.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE scans ADD COLUMN {col} {typ}"))
        else:
            rows = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='scans'")).fetchall()
            existing = {row[0] for row in rows}
            for col, typ in needed.items():
                if col not in existing:
                    if col == "purchase_date":
                        conn.execute(text("ALTER TABLE scans ADD COLUMN purchase_date TIMESTAMP WITH TIME ZONE"))
                    else:
                        conn.execute(text(f"ALTER TABLE scans ADD COLUMN {col} {typ}"))
ensure_scan_extra_columns()

# ---------------- App ----------------
app = FastAPI(title="QR Full (FastAPI + PWA + NFC-e)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

class QRText(BaseModel):
    text: str
    source: Optional[str] = "pwa"

# ---------------- Utils ----------------
def parse_br_decimal(s: str) -> Optional[Decimal]:
    if s is None:
        return None
    s = s.strip()
    s = re.sub(r'[^\d,.-]', '', s)
    if s.count(',') > 1 and s.count('.') == 0:
        return None
    s = s.replace('.', '').replace(',', '.')
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None

def parse_br_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def looks_like_nfce_url(text: str) -> bool:
    return (
        ("http://" in text or "https://" in text)
        and ("sefaz" in text.lower() or "fazenda" in text.lower())
        and ("p=" in text or "chNFe" in text or "chave" in text.lower())
    )

def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except Exception:
        return False

# ----------- Scraper NFC-e (meta + itens) -----------
def _fetch_nfce_page(qr_url: str, timeout: float = 15.0):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    try:
        r = requests.get(qr_url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception:
        return None, ""
    if r.status_code != 200:
        return None, ""
    soup = BeautifulSoup(r.text, "lxml") if _has_lxml() else BeautifulSoup(r.text, "html.parser")
    text_all = " ".join(s.strip() for s in soup.stripped_strings)
    return soup, text_all

def fetch_nfce_items(qr_url: str, timeout: float = 15.0) -> List[dict]:
    soup, text_all = _fetch_nfce_page(qr_url, timeout=timeout)
    if not soup:
        return []
    # 1) Tabela típica (Descrição / Qtde / Valor unitário / Valor total)
    tables = soup.find_all("table")
    wanted_headers = ["descrição", "descricao", "qtde", "valor unitário", "valor unitario", "valor total"]
    for table in tables:
        ths = [th.get_text(strip=True).lower() for th in table.find_all(["th", "td"], recursive=True)][:8]
        score = sum(1 for h in wanted_headers if any(h in th for th in ths))
        if score >= 3:
            header_row = None
            for tr in table.find_all("tr"):
                cols = [c.get_text(strip=True).lower() for c in tr.find_all(["th", "td"])]
                if any("descri" in c for c in cols) and any("valor" in c for c in cols):
                    header_row = tr
                    break
            if not header_row:
                continue
            header_cols = [c.get_text(strip=True).lower() for c in header_row.find_all(["th", "td"])]
            def find_idx(keys):
                for i, c in enumerate(header_cols):
                    for k in keys:
                        if k in c:
                            return i
                return None
            idx_name = find_idx(["descri", "produto", "mercadoria", "item"])
            idx_qty = find_idx(["qtde", "quant"])
            idx_unit = find_idx(["valor unit", "vl unit", "unitário", "unitario"])
            idx_total = find_idx(["valor total", "vl total", "total"])

            items = []
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                if [td.get_text(strip=True).lower() for td in tds] == header_cols:
                    continue
                name = tds[idx_name].get_text(strip=True) if idx_name is not None and idx_name < len(tds) else None
                qty = parse_br_decimal(tds[idx_qty].get_text(strip=True)) if idx_qty is not None and idx_qty < len(tds) else None
                unit_price = parse_br_decimal(tds[idx_unit].get_text(strip=True)) if idx_unit is not None and idx_unit < len(tds) else None
                total_price = parse_br_decimal(tds[idx_total].get_text(strip=True)) if idx_total is not None and idx_total < len(tds) else None
                if name and (unit_price is not None or total_price is not None):
                    items.append({"name": name, "qty": qty, "unit_price": unit_price, "total_price": total_price})
            if items:
                return items
    # 2) Fallback: regex no texto corrido
    regex = re.compile(
        r"(?:\d+\s*-\s*)?(?P<name>[^|]+?)\s+Qtde\.?:\s*(?P<qty>[\d\.,]+)\s+Valor\s+unit[áa]rio\s*R?\$?\s*(?P<unit>[\d\.,]+)\s+Valor\s+total\s*R?\$?\s*(?P<total>[\d\.,]+)",
        flags=re.IGNORECASE
    )
    items = []
    for m in regex.finditer(text_all):
        name = m.group("name").strip()
        qty = parse_br_decimal(m.group("qty"))
        unit_price = parse_br_decimal(m.group("unit"))
        total_price = parse_br_decimal(m.group("total"))
        if name and (unit_price is not None or total_price is not None):
            items.append({"name": name, "qty": qty, "unit_price": unit_price, "total_price": total_price})
    return items

def fetch_nfce_meta(qr_url: str, timeout: float = 15.0) -> Dict[str, Any]:
    soup, text_all = _fetch_nfce_page(qr_url, timeout=timeout)
    if not soup:
        return {}
    meta: Dict[str, Any] = {"cnpj": None, "store_name": None, "purchase_date": None}

    # CNPJ
    m = re.search(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", text_all)
    if m:
        meta["cnpj"] = m.group(0)

    # Loja / Razão Social / Emitente (heurística)
    labels = ["Razão Social", "Razao Social", "Nome/Razão Social", "Nome / Razão Social", "Emitente", "Estabelecimento", "Nome"]
    def find_label_value():
        for lab in labels:
            el = soup.find(lambda tag: tag.name in ["td","th","span","div","label","strong","b"] and lab.lower() in tag.get_text(" ", strip=True).lower())
            if el:
                sib = el.find_next(string=True)
                if sib:
                    sv = str(sib).strip()
                    if len(sv) > 2 and not any(k in sv.lower() for k in ["cnpj", "cpf", "emissão", "emissao"]):
                        return sv
                nxt = el.find_next()
                if nxt and nxt is not el:
                    sv = nxt.get_text(" ", strip=True)
                    if sv and len(sv) > 2:
                        return sv
        return None
    store = find_label_value()
    if store:
        store = re.split(r"\s{2,}", store)[0]
        meta["store_name"] = store

    # Data de emissão
    dm = re.search(r"(Emiss[aã]o|Data\s+de\s+Emiss[aã]o)\s*[:\-]?\s*(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)", text_all, flags=re.IGNORECASE)
    if dm:
        dt = parse_br_datetime(dm.group(2))
        if dt:
            meta["purchase_date"] = dt

    return meta

# ---------------- Rotas PWA ----------------
@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse("static/manifest.webmanifest")

@app.get("/sw.js")
def sw():
    return FileResponse("static/sw.js", media_type="text/javascript")

# ---------------- API ----------------
@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _save_scan_and_nfce_items(raw_text: str, source: str) -> dict:
    session = SessionLocal()
    try:
        scan = Scan(source=source or "pwa", data_raw=raw_text)
        session.add(scan)
        session.flush()  # garante scan.id

        items_saved = 0
        if looks_like_nfce_url(raw_text):
            meta = fetch_nfce_meta(raw_text)
            if meta:
                scan.cnpj = meta.get("cnpj")
                scan.store_name = meta.get("store_name")
                pd = meta.get("purchase_date")
                if isinstance(pd, datetime):
                    scan.purchase_date = pd

            items = fetch_nfce_items(raw_text)
            for it in items:
                session.add(ScanItem(
                    scan_id=scan.id,
                    name=it.get("name") or "",
                    qty=it.get("qty"),
                    unit_price=it.get("unit_price"),
                    total_price=it.get("total_price"),
                ))
            items_saved = len(items)

        session.commit()
        return {"scan_id": scan.id, "items_saved": items_saved}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

@app.post("/save_text")
def save_text(payload: QRText):
    try:
        result = _save_scan_and_nfce_items(payload.text, payload.source or "pwa")
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/scan")
async def scan_image(file: UploadFile = File(...), source: Optional[str] = Query(default="upload")):
    if not file.content_type or "image" not in file.content_type:
        raise HTTPException(status_code=400, detail="Envie uma imagem (content-type image/*).")
    data = await file.read()
    texts = decode_qr_bytes(data)
    if not texts:
        return {"found": 0, "items": []}

    results = []
    for t in texts:
        info = _save_scan_and_nfce_items(t, source or (file.filename or "upload"))
        results.append({"text": t, **info})
    return {"found": len(texts), "items": results}

@app.get("/scan/{scan_id}")
def get_scan(scan_id: int):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, timestamp, source, data_raw, cnpj, store_name, purchase_date FROM scans WHERE id = :sid"),
            {"sid": scan_id}
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Scan não encontrado")
        items = conn.execute(
            text("SELECT id, name, qty, unit_price, total_price FROM scan_items WHERE scan_id = :sid ORDER BY id ASC"),
            {"sid": scan_id}
        ).mappings().all()

    def ts_to_str(ts):
        if ts is None:
            return None
        if isinstance(ts, str):
            return ts
        return ts.isoformat()

    return {
        "scan": {
            "id": row["id"],
            "timestamp": ts_to_str(row["timestamp"]),
            "source": row["source"],
            "data_raw": row["data_raw"],
            "cnpj": row["cnpj"],
            "store_name": row["store_name"],
            "purchase_date": ts_to_str(row["purchase_date"]),
        },
        "items": [
            {
                "id": it["id"],
                "name": it["name"],
                "qty": str(it["qty"]) if it["qty"] is not None else None,
                "unit_price": str(it["unit_price"]) if it["unit_price"] is not None else None,
                "total_price": str(it["total_price"]) if it["total_price"] is not None else None,
            } for it in items
        ]
    }

@app.get("/list")
def list_scans(limit: int = 100, q: Optional[str] = None):
    sql = "SELECT id, timestamp, source, data_raw, cnpj, store_name, purchase_date FROM scans"
    params = {}
    if q:
        sql += " WHERE data_raw LIKE :q"
        params["q"] = f"%{q}%"
    sql += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    def ts_to_str(ts):
        if ts is None:
            return None
        if isinstance(ts, str):
            return ts
        return ts.isoformat()

    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "timestamp": ts_to_str(r["timestamp"]),
            "source": r["source"],
            "data_raw": r["data_raw"],
            "cnpj": r["cnpj"],
            "store_name": r["store_name"],
            "purchase_date": ts_to_str(r["purchase_date"]),
        })
    return {"count": len(items), "items": items}

@app.get("/items")
def list_items(scan_id: Optional[int] = None, limit: int = 200):
    sql = "SELECT id, scan_id, name, qty, unit_price, total_price FROM scan_items"
    params = {}
    if scan_id:
        sql += " WHERE scan_id = :sid"
        params["sid"] = scan_id
    sql += " ORDER BY id DESC LIMIT :lim"
    params["lim"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    def d(v):
        if v is None:
            return None
        return str(v)

    return {
        "count": len(rows),
        "items": [
            {
                "id": r["id"],
                "scan_id": r["scan_id"],
                "name": r["name"],
                "qty": d(r["qty"]),
                "unit_price": d(r["unit_price"]),
                "total_price": d(r["total_price"]),
            } for r in rows
        ]
    }

@app.get("/download")
def download_csv():
    def row_iter():
        yield ("id;timestamp;source;data_raw;cnpj;store_name;purchase_date\n").encode("utf-8")
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text("SELECT id, timestamp, source, data_raw, cnpj, store_name, purchase_date FROM scans ORDER BY id ASC")
            )
            for r in result:
                def ts_to_str(ts):
                    if ts is None:
                        return ""
                    if isinstance(ts, str):
                        return ts
                    return ts.isoformat()
                line = [
                    str(r.id),
                    ts_to_str(r.timestamp),
                    (r.source or ""),
                    (r.data_raw or "").replace("\n", " ").replace("\r", " "),
                    (r.cnpj or ""),
                    (r.store_name or "").replace("\n", " ").replace("\r", " "),
                    ts_to_str(r.purchase_date),
                ]
                yield (";".join(line) + "\n").encode("utf-8")
    return StreamingResponse(
        row_iter(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scans.csv"'},
    )

@app.get("/download_items")
def download_items_csv():
    def row_iter():
        yield ("id;scan_id;name;qty;unit_price;total_price\n").encode("utf-8")
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text("SELECT id, scan_id, name, qty, unit_price, total_price FROM scan_items ORDER BY id ASC")
            )
            for r in result:
                def to_s(v):
                    if v is None:
                        return ""
                    return str(v).replace("\n", " ").replace("\r", " ")
                line = [
                    str(r.id),
                    str(r.scan_id),
                    to_s(r.name),
                    to_s(r.qty),
                    to_s(r.unit_price),
                    to_s(r.total_price),
                ]
                yield (";".join(line) + "\n").encode("utf-8")
    return StreamingResponse(
        row_iter(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scan_items.csv"'},
    )
