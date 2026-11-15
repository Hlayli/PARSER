"""HTTP-приложение PARSER: API + статика + SSE."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .adapters.registry import all_adapters
from .categories import catalog, resolve_queries
from .scanner import SCANNER, make_grid, reset_stale_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("parser")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    reset_stale_jobs()
    log.info("PARSER started, db=%s", db.DB_PATH)
    yield


app = FastAPI(title="PARSER", version="1.0.0", lifespan=lifespan)

if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


# --- схемы -----------------------------------------------------------------

class ScanReq(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    categories: list[str] = []
    city: str | None = None
    bbox: list[float] | None = None
    cell_deg: float = 0.05
    sources: list[str] = ["google", "yandex", "2gis"]
    limit_per_cell: int = 40


class EstimateReq(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    categories: list[str] = []
    city: str | None = None
    bbox: list[float] | None = None
    cell_deg: float = 0.05


class LeadPatch(BaseModel):
    status: str | None = None
    is_favorite: bool | None = None
    note: str | None = None


# --- служебное --------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    adapters = []
    for name, adapter in all_adapters().items():
        try:
            ok, reason = adapter.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, str(exc)
        adapters.append({"name": name, "label": adapter.label,
                         "available": bool(ok), "reason": reason})
    return {"ok": True, "adapters": adapters,
            "demo": any(a["name"] == "demo" and a["available"] for a in adapters)}


@app.get("/api/stats")
async def stats() -> dict:
    with db.connect() as conn:
        return db.stats(conn)


# --- категории --------------------------------------------------------------

@app.get("/api/categories")
async def categories() -> dict:
    """Каталог ниш для меню «Что ищем»."""
    return catalog()


# --- сканирование -----------------------------------------------------------

async def _bbox_from_payload(city: str | None, bbox_raw: list[float] | None):
    """Разобрать bbox из запроса или геокодировать город."""
    if bbox_raw:
        if len(bbox_raw) != 4:
            raise HTTPException(400, "bbox должен содержать 4 числа")
        return tuple(float(x) for x in bbox_raw)
    if city:
        from .geocode import city_bbox  # ленивый импорт

        try:
            bbox, _display = await city_bbox(city)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"не удалось определить город: {exc}") from exc
        return bbox
    raise HTTPException(400, "нужен city или bbox")


@app.post("/api/scan/estimate")
async def estimate_scan(req: EstimateReq) -> dict:
    """Сколько запросов будет в скане: категории × квадраты."""
    try:
        queries = resolve_queries(req.categories, req.query)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    bbox = await _bbox_from_payload(req.city, req.bbox)
    cells = len(make_grid(bbox, req.cell_deg))
    return {
        "queries": len(queries),
        "cells": cells,
        "requests": len(queries) * cells,
        "bbox": list(bbox),
    }


@app.post("/api/scan")
async def start_scan(req: ScanReq) -> dict:
    try:
        queries = resolve_queries(req.categories, req.query)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not queries:
        raise HTTPException(400, "выберите хотя бы одну категорию или укажите свой запрос")

    bbox = await _bbox_from_payload(req.city, req.bbox)

    sources = [s for s in req.sources if s]
    try:
        job_id = SCANNER.start(queries, bbox, req.cell_deg, sources,
                               req.limit_per_cell, city=req.city)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    cells = len(make_grid(bbox, req.cell_deg))
    return {
        "job_id": job_id,
        "cells": cells,
        "queries": len(queries),
        "bbox": list(bbox),
    }


@app.get("/api/jobs")
async def jobs() -> dict:
    return {"items": SCANNER.jobs()}


@app.get("/api/jobs/{job_id}")
async def job(job_id: str) -> dict:
    result = SCANNER.job(job_id)
    if not result:
        raise HTTPException(404, "задание не найдено")
    return result


@app.post("/api/jobs/{job_id}/pause")
async def pause(job_id: str) -> dict:
    if not SCANNER.job(job_id):
        raise HTTPException(404, "задание не найдено")
    SCANNER.pause(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
async def resume(job_id: str) -> dict:
    if not SCANNER.job(job_id):
        raise HTTPException(404, "задание не найдено")
    SCANNER.resume(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/stop")
async def stop(job_id: str) -> dict:
    if not SCANNER.job(job_id):
        raise HTTPException(404, "задание не найдено")
    SCANNER.stop(job_id)
    return {"ok": True}


# --- лиды -------------------------------------------------------------------

@app.get("/api/leads")
async def leads(
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    fav: bool = False,
    sort: str = "new",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    with db.connect() as conn:
        total, items = db.list_leads(conn, status=status, source=source, q=q,
                                     fav=fav, sort=sort, limit=limit, offset=offset)
    return {"total": total, "items": items}


@app.get("/api/leads/{business_id}")
async def lead(business_id: str) -> dict:
    with db.connect() as conn:
        result = db.get_lead(conn, business_id)
    if not result:
        raise HTTPException(404, "лид не найден")
    return result


@app.patch("/api/leads/{business_id}")
async def patch_lead(business_id: str, body: LeadPatch) -> dict:
    with db.connect() as conn:
        try:
            ok = db.set_status(conn, business_id, status=body.status,
                               is_favorite=body.is_favorite, note=body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not ok:
            raise HTTPException(404, "лид не найден")
        result = db.get_lead(conn, business_id)
    return {"ok": True, "lead": result}


@app.get("/api/export.csv")
async def export_csv(
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    fav: bool = False,
) -> Response:
    import csv
    import io

    with db.connect() as conn:
        all_items: list[dict] = []
        offset = 0
        while True:
            total, items = db.list_leads(conn, status=status, source=source, q=q,
                                         fav=fav, sort="name", limit=500, offset=offset)
            all_items.extend(items)
            offset += len(items)
            if not items or offset >= total:
                break
        rows = []
        for item in all_items:
            full = db.get_lead(conn, item["id"]) or {}
            facts = full.get("facts", {})
            rows.append({
                "Название": item["name"],
                "Категория": item["category"],
                "Адрес": item["address"],
                "Телефон": item["phone"],
                "Сайт": item["website"],
                "Рейтинг": item["rating"] if item["rating"] is not None else "",
                "Отзывов": item["reviews_count"] if item["reviews_count"] is not None else "",
                "Цены": "; ".join(f["value"] for f in facts.get("price", [])),
                "Email": "; ".join(f["value"] for f in facts.get("email", [])),
                "Соцсети": "; ".join(f["value"] for f in facts.get("social", [])),
                "Руководитель": "; ".join(f["value"] for f in facts.get("director", [])),
                "ИНН": "; ".join(f["value"] for f in facts.get("inn", [])),
                "Источники": ", ".join(item["sources"]),
                "Статус": item["status"],
                "Избранное": "да" if item["is_favorite"] else "",
                "Заметка": item["note"],
            })

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()) if rows else
                            ["Название", "Категория", "Адрес", "Телефон", "Сайт"])
    writer.writeheader()
    writer.writerows(rows)
    payload = "\ufeff" + buffer.getvalue()
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="parser-leads.csv"'},
    )


@app.post("/api/leads/{business_id}/enrich")
async def enrich_lead(business_id: str) -> dict:
    with db.connect() as conn:
        if not db.get_lead(conn, business_id):
            raise HTTPException(404, "лид не найден")
    try:
        from .enrichment import enrich
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"модуль обогащения недоступен: {exc}") from exc

    result = await enrich(business_id)
    with db.connect() as conn:
        result["lead"] = db.get_lead(conn, business_id)
    return result


# --- SSE --------------------------------------------------------------------

def _sse(event: dict) -> str:
    import json

    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = SCANNER.subscribe()

    async def stream():
        try:
            yield _sse({"type": "hello"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse(event)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            SCANNER.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- фронтенд ---------------------------------------------------------------

@app.get("/")
async def index():
    path = FRONTEND / "index.html"
    if not path.exists():
        return JSONResponse(
            {"error": "frontend/index.html не найден", "api": "/api/health"},
            status_code=503,
        )
    return FileResponse(path)
