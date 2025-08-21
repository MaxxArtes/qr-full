# app.py
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, List

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dateutil.tz import tzlocal

from sqlalchemy import (
    create_engine, text, Column, Integer, Text, DateTime, func, Numeric
)
from sqlalchemy.orm import sessionmaker, declarative_base

from qr_utils import decode_qr_bytes

# ---------------- Config ----------------
# Dev local: sqlite:///./dev.db
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

class ScanItem(Base):
    __tablename__ = "scan_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(Integer, nullable=False)
    name = Column(Text, nullable=False)
    qty = Column(Numeric(14, 4))         # ex.: 1.0000
    unit_price = Column(Numeric(14, 4))  # ex.: 9.9900
    total_price = Column(Numeric(14, 2)) # ex.: 9.99

Base.metadata.create_all(engine)

# ---------------- App ----------------
app = FastAPI(title="QR Full (FastAPI + PWA + NFC-e)")

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

# ---------------- Util: parse moeda/quantidade ----------------
def parse_br_decimal(s: str) -> Optional[Decimal]:
    """
    Converte 'R$ 1.234,56' -> Decimal('1234.56').
    Aceita '1,00', '2.345,7', etc. Retorna None se não conseguir.
    """
    if s is None:
        return None
    s = s.strip()
    s = re.sub(r'[^\d,.-]', '', s)  # remove R$, espaços, etc
    if s.count(',') > 1 and s.count('.') == 0:
        # algo muito estranho, deixa pra trás
        return None
    # remove separador de milhar '.' e troca ',' por '.'
    s = s.replace('.', '').replace(',', '.')
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None

def looks_like_nfce_url(text: str) -> bool:
    """
    Heurística simples: URLs de QR de NFC-e da SEFAZ
    - geralmente têm 'sefaz' no host e param 'p=' com a chave
    - /consultaNFCe ou /QRCode etc.
    """
    return (
        ("http://" in text or "https://" in text)
        and ("sefaz" in text.lower() or "fazenda" in text.lower())
        and ("p=" in text or "chNFe" in text or "chave" in text.lower())
    )

# ---------------- Scraper NFC-e ----------------
def fetch_nfce_items(qr_url: str, timeout: float = 15.0) -> List[dict]:
    """
    Baixa a página pública do QR da NFC-e e extrai itens.
    Retorna lista: [{name, qty, unit_price, total_price}, ...]
    Observação: o HTML varia por estado e versão, então tentamos múltiplas estratégias.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                      " Chrome/124.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    try:
        r = requests.get(qr_url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    html = r.text
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    text_all = " ".join(s.strip() for s in soup.stripped_strings)

    # 1) Padrão comum: tabela com cabeçalhos "Descrição", "Qtde", "Valor unitário", "Valor total"
    # Procurar primeira tabela que tenha esses headers
    tables = soup.find_all("table")
    wanted_headers = ["descrição", "descricao", "qtde", "valor unitário", "valor unitario", "valor total"]
    for table in tables:
        ths = [th.get_text(strip=True).lower() for th in table.find_all(["th", "td"], recursive=True)][:8]
        score = sum(1 for h in wanted_headers if any(h in th for th in ths))
        if score >= 3:
            # tenta mapear colunas por header
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
                # pular a linha de header se cair aqui
                if [td.get_text(strip=True).lower() for td in tds] == header_cols:
                    continue
                name = tds[idx_name].get_text(strip=True) if idx_name is not None and idx_name < len(tds) else None
                qty = parse_br_decimal(tds[idx_qty].get_text(strip=True)) if idx_qty is not None and idx_qty < len(tds) else None
                unit_price = parse_br_decimal(tds[idx_unit].get_text(strip=True)) if idx_unit is not None and idx_unit < len(tds) else None
                total_price = parse_br_decimal(tds[idx_total].get_text(strip=True)) if idx_total is not None and idx_total < len(tds) else None
                # aceita se tiver pelo menos nome + alguma info de preço
                if name and (unit_price is not None or total_price is not None):
                    items.append({
                        "name": name,
                        "qty": qty,
                        "unit_price": unit_price,
                        "total_price": total_price
                    })
            if items:
                return items

    # 2) Fallback por REGEX no texto completo (bastante comum em vários portais consumer)
    # Padrões típicos no consumer: "... 1 - NOME DO PRODUTO Qtde.: 1,0000 Valor unitário R$ 9,99 Valor total R$ 9,99 ..."
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
            items.append({
                "name": name,
                "qty": qty,
                "unit_price": unit_price,
                "total_price": total_price
            })
    if items:
        return items

    # 3) Sem itens encontrados
    return []

def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except Exception:
        return False

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
    """
    Cria o Scan e, se for NFC-e, extrai itens e salva ScanItem.
    Retorna dict com info do scan e quantos itens salvos.
    """
    session = SessionLocal()
    try:
        scan = Scan(source=source or "pwa", data_raw=raw_text)
        session.add(scan)
        session.flush()  # garante scan.id

        items_saved = 0
        if looks_like_nfce_url(raw_text):
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
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()

@app.post("/save_text")
def save_text(payload: QRText):
    """Salva texto; se for URL de NFC-e, busca itens e grava."""
    try:
        result = _save_scan_and_nfce_items(payload.text, payload.source or "pwa")
        return {"ok": True, **result}
    except Exception as e:
        # não vazar exceção crua pro cliente em prod; aqui mantemos simples
        return {"ok": False, "error": str(e)}

@app.post("/scan")
async def scan_image(file: UploadFile = File(...), source: Optional[str] = Query(default="upload")):
    """Recebe imagem, decodifica QRs e salva; se achar NFC-e, extrai itens."""
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

@app.get("/list")
def list_scans(limit: int = 100, q: Optional[str] = None):
    """Lista últimos registros; filtro opcional com ?q=trecho."""
    sql = "SELECT id, timestamp, source, data_raw FROM scans"
    params = {}
    if q:
        sql += " WHERE data_raw LIKE :q"
        params["q"] = f"%{q}%"
    sql += " ORDER BY id DESC LIMIT :limit"
    params["limit"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items = []
    for r in rows:
        ts = r["timestamp"]
        if ts is None:
            ts_str = None
        elif isinstance(ts, str):
            ts_str = ts
        else:
            ts_str = ts.isoformat()
        items.append({
            "id": r["id"],
            "timestamp": ts_str,
            "source": r["source"],
            "data_raw": r["data_raw"],
        })
    return {"count": len(items), "items": items}

@app.get("/items")
def list_items(scan_id: Optional[int] = None, limit: int = 200):
    """Lista itens extraídos. Use ?scan_id= para filtrar por um scan específico."""
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
        # Converte Decimal/None para str ou vazio
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
    """Exporta SCANS em CSV."""
    def row_iter():
        yield ("id;timestamp;source;data_raw\n").encode("utf-8")
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                text("SELECT id, timestamp, source, data_raw FROM scans ORDER BY id ASC")
            )
            for r in result:
                ts = r.timestamp
                if ts is None:
                    ts_str = ""
                elif isinstance(ts, str):
                    ts_str = ts
                else:
                    ts_str = ts.isoformat()

                line = [
                    str(r.id),
                    ts_str,
                    (r.source or ""),
                    (r.data_raw or "").replace("\n", " ").replace("\r", " ")
                ]
                yield (";".join(line) + "\n").encode("utf-8")

    return StreamingResponse(
        row_iter(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="scans.csv' + '"'},
    )

@app.get("/download_items")
def download_items_csv():
    """Exporta ITENS extraídos em CSV."""
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
