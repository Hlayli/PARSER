"""Движок сканирования: сетка квадратов, параллельные адаптеры, SSE-события."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from typing import Any

from . import db
from .adapters.registry import all_adapters, get_adapters
from .models import BBox, Place

log = logging.getLogger(__name__)

MAX_CELLS = 2000
CELL_CONCURRENCY = 3
CELL_TIMEOUT = 180
GLOBAL_TIMEOUT = 300  # 2ГИС обходит весь город одним прогоном


def make_grid(bbox: BBox, cell_deg: float) -> list[tuple[int, int, BBox]]:
    """Нарезать bbox на квадраты cell_deg. Возвращает [(ix, iy, bbox), ...]."""
    min_lon, min_lat, max_lon, max_lat = bbox
    cell_deg = max(0.002, min(float(cell_deg or 0.02), 2.0))
    nx = max(1, math.ceil((max_lon - min_lon) / cell_deg - 1e-9))
    ny = max(1, math.ceil((max_lat - min_lat) / cell_deg - 1e-9))

    while nx * ny > MAX_CELLS:
        cell_deg *= 1.25
        nx = max(1, math.ceil((max_lon - min_lon) / cell_deg - 1e-9))
        ny = max(1, math.ceil((max_lat - min_lat) / cell_deg - 1e-9))

    step_x = (max_lon - min_lon) / nx
    step_y = (max_lat - min_lat) / ny
    cells = []
    for iy in range(ny):
        for ix in range(nx):
            cells.append((
                ix, iy,
                (min_lon + ix * step_x, min_lat + iy * step_y,
                 min_lon + (ix + 1) * step_x, min_lat + (iy + 1) * step_y),
            ))
    return cells


class ScanManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._paused: set[str] = set()
        self._stopped: set[str] = set()
        self._queues: set[asyncio.Queue] = set()
        self._new_counts: dict[str, int] = {}

    # --- подписка на события ------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def _emit(self, event: dict[str, Any]) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # --- управление заданиями ----------------------------------------------
    def start(self, queries: str | list[str], bbox: BBox, cell_deg: float,
              sources: list[str], limit_per_cell: int = 40,
              city: str | None = None) -> str:
        normalized = self._normalize_queries(queries)
        if not normalized:
            raise ValueError("пустой запрос")
        min_lon, min_lat, max_lon, max_lat = bbox
        if not (min_lon < max_lon and min_lat < max_lat):
            raise ValueError("некорректный bbox")

        adapters = get_adapters(sources)
        if not adapters:
            reasons = []
            for name in sources:
                adapter = all_adapters().get(name)
                if adapter is None:
                    reasons.append(f"{name}: неизвестный источник")
                    continue
                ok, reason = adapter.available()
                reasons.append(f"{adapter.label or name}: {reason or 'недоступен'}")
            raise ValueError(
                "ни один источник не доступен — " + "; ".join(reasons)
            )

        # 2ГИС ищет по городу целиком; город задания передаём в отдельную
        # копию адаптера, чтобы параллельные задания не перепутали города.
        city_value = (city or "").strip()
        if city_value:
            adapters = [
                adapter.with_city(city_value)
                if adapter.name == "2gis" and hasattr(adapter, "with_city")
                else adapter
                for adapter in adapters
            ]

        cells = make_grid(bbox, cell_deg)
        job_id = uuid.uuid4().hex[:12]
        self._new_counts[job_id] = 0
        ts = db.now()

        with db.connect() as conn:
            conn.execute(
                """INSERT INTO scan_job (id, query, queries_json, query_index, city, bbox_json,
                       cell_deg, sources_json, status,
                       cells_total, cells_done, leads_found, error, created_at, updated_at)
                   VALUES (?,?,?,0,?,?,?,?, 'queued', ?, 0, 0, '', ?, ?)""",
                (job_id, self._summary(normalized), json.dumps(normalized, ensure_ascii=False),
                 city_value, json.dumps(bbox), cell_deg,
                 json.dumps([a.name for a in adapters]), len(cells), ts, ts),
            )
            conn.executemany(
                "INSERT INTO scan_cell (job_id, ix, iy, bbox_json, status) VALUES (?,?,?,?, 'pending')",
                [(job_id, ix, iy, json.dumps(cb)) for ix, iy, cb in cells],
            )
            conn.commit()

        self._emit({"type": "job", "job_id": job_id, "status": "queued",
                    "cells_total": len(cells), "cells_done": 0, "leads_found": 0,
                    "queries_total": len(normalized), "query_index": 0,
                    "current_query": normalized[0]})

        self._tasks[job_id] = asyncio.create_task(
            self._run(job_id, normalized, bbox, adapters, limit_per_cell)
        )
        log.info("job %s started: %s queries, %s cells, sources=%s", job_id,
                 len(normalized), len(cells), [a.name for a in adapters])
        return job_id

    @staticmethod
    def _normalize_queries(queries: str | list[str]) -> list[str]:
        """Убрать пустые и повторяющиеся запросы, сохранив порядок."""
        if isinstance(queries, str):
            queries = [queries]
        result: list[str] = []
        seen: set[str] = set()
        for raw in queries:
            query = " ".join(str(raw or "").split())
            if not query:
                continue
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(query)
        if len(result) > 300:
            raise ValueError("слишком много запросов: максимум 300 за один скан")
        return result

    @staticmethod
    def _summary(queries: list[str]) -> str:
        """Короткое описание задания для списка истории."""
        if len(queries) == 1:
            return queries[0]
        head = ", ".join(queries[:3])
        tail = "…" if len(queries) > 3 else ""
        return f"{len(queries)} категорий: {head}{tail}"

    def pause(self, job_id: str) -> None:
        self._paused.add(job_id)
        self._set_job(job_id, status="paused")
        self._emit({"type": "job", "job_id": job_id, "status": "paused"})

    def resume(self, job_id: str) -> None:
        self._paused.discard(job_id)
        self._set_job(job_id, status="running")
        self._emit({"type": "job", "job_id": job_id, "status": "running"})
        task = self._tasks.get(job_id)
        if task is None or task.done():
            # задание было остановлено — добираем оставшиеся ячейки
            self._stopped.discard(job_id)
            job = self.job(job_id)
            if job and job["status"] not in ("done",):
                self._resume_task(job_id)

    def _resume_task(self, job_id: str) -> None:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM scan_job WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return
        adapters = [a for a in get_adapters(json.loads(row["sources_json"])) if a.available()[0]]
        city = row["city"] if "city" in row.keys() else ""
        if city:
            adapters = [
                adapter.with_city(city)
                if adapter.name == "2gis" and hasattr(adapter, "with_city")
                else adapter
                for adapter in adapters
            ]
        queries = self._job_queries(row)
        self._tasks[job_id] = asyncio.create_task(
            self._run(job_id, queries, tuple(json.loads(row["bbox_json"])), adapters, 40)
        )

    @staticmethod
    def _job_queries(row) -> list[str]:
        """Список запросов задания с совместимостью со старыми строками."""
        raw = row["queries_json"] if "queries_json" in row.keys() else "[]"
        try:
            queries = json.loads(raw or "[]")
        except (TypeError, ValueError):
            queries = []
        if not isinstance(queries, list) or not queries:
            queries = [row["query"]]
        return ScanManager._normalize_queries(queries)

    def stop(self, job_id: str) -> None:
        self._stopped.add(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        self._set_job(job_id, status="stopped")
        self._emit({"type": "job", "job_id": job_id, "status": "stopped"})

    def job(self, job_id: str) -> dict | None:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM scan_job WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            cells = [
                [c["ix"], c["iy"], c["status"], c["found"], c["error"]]
                for c in conn.execute(
                    "SELECT ix, iy, status, found, error FROM scan_cell WHERE job_id = ? "
                    "ORDER BY iy, ix", (job_id,)
                )
            ]
        queries = self._job_queries(row)
        query_index = row["query_index"] if "query_index" in row.keys() else 0
        query_index = max(0, min(int(query_index or 0), max(0, len(queries) - 1)))
        return {
            "id": row["id"], "query": row["query"],
            "queries": queries,
            "queries_total": len(queries),
            "query_index": query_index,
            "current_query": queries[query_index] if queries else "",
            "city": row["city"] if "city" in row.keys() else "",
            "bbox": json.loads(row["bbox_json"]),
            "cell_deg": row["cell_deg"],
            "sources": json.loads(row["sources_json"]),
            "status": row["status"],
            "cells_total": row["cells_total"],
            "cells_done": row["cells_done"],
            "leads_found": row["leads_found"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "cells": cells,
        }

    def jobs(self) -> list[dict]:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, query, status, cells_total, cells_done, leads_found, created_at "
                "FROM scan_job ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        return [dict(r) for r in rows]

    def _set_job(self, job_id: str, status: str | None = None, **fields: Any) -> None:
        sets, params = [], []
        if status:
            sets.append("status = ?")
            params.append(status)
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([db.now(), job_id])
        with db.connect() as conn:
            conn.execute(f"UPDATE scan_job SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()

    # --- основной цикл ------------------------------------------------------
    async def _run(self, job_id: str, queries: list[str], bbox: BBox,
                   adapters: list, limit_per_cell: int) -> None:
        self._set_job(job_id, status="running")
        self._emit({"type": "job", "job_id": job_id, "status": "running",
                    "queries_total": len(queries), "query_index": 0,
                    "current_query": queries[0]})

        # источники без bbox (2ГИС-браузер) обрабатывают весь город одним прогоном
        global_adapters = [a for a in adapters if not a.supports_bbox]
        local_adapters = [a for a in adapters if a.supports_bbox]

        with db.connect() as conn:
            row = conn.execute(
                "SELECT query_index FROM scan_job WHERE id = ?", (job_id,)
            ).fetchone()
        start_index = 0
        if row and row["query_index"] is not None:
            start_index = max(0, min(int(row["query_index"]), len(queries) - 1))

        for index in range(start_index, len(queries)):
            if job_id in self._stopped:
                break
            query = queries[index]
            if index > start_index:
                # Новый запрос — новая волна квадратов. Найденное по прошлой
                # категории остаётся в базе, сетка показывает текущую волну.
                self._reset_cells(job_id)
                self._set_job(job_id, query_index=index, cells_done=0)
            else:
                self._set_job(job_id, query_index=index)

            counters = self._counters(job_id)
            self._emit({
                "type": "job", "job_id": job_id, "status": "running",
                "queries_total": len(queries), "query_index": index,
                "current_query": query,
                "reset_cells": index > start_index,
                **counters,
            })

            # Городские источники запускаем параллельно с квадратами: иначе
            # Google и Яндекс не покажут ничего, пока 2ГИС обходит весь город.
            global_tasks = [
                asyncio.create_task(self._run_global(job_id, adapter, query, bbox))
                for adapter in global_adapters
            ]

            sem = asyncio.Semaphore(CELL_CONCURRENCY)

            async def handle(cell_row: dict) -> None:
                async with sem:
                    if job_id in self._stopped:
                        return
                    while job_id in self._paused:
                        await asyncio.sleep(0.4)
                        if job_id in self._stopped:
                            return
                    await self._scan_cell(job_id, cell_row, query, local_adapters, limit_per_cell)

            with db.connect() as conn:
                cells = [
                    dict(r) for r in conn.execute(
                        "SELECT id, ix, iy, bbox_json FROM scan_cell "
                        "WHERE job_id = ? AND status IN ('pending','error') ORDER BY iy, ix",
                        (job_id,),
                    )
                ]

            try:
                await asyncio.gather(*(handle(c) for c in cells))
            except asyncio.CancelledError:
                for task in global_tasks:
                    task.cancel()
                log.info("job %s cancelled", job_id)
                return

            if not local_adapters:
                # источники без bbox уже отработали — ячейки помечаем выполненными
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE scan_cell SET status='done', finished_at=? "
                        "WHERE job_id=? AND status IN ('pending','error')",
                        (db.now(), job_id),
                    )
                    conn.commit()

            if job_id in self._stopped:
                for task in global_tasks:
                    task.cancel()
            if global_tasks:
                await asyncio.gather(*global_tasks, return_exceptions=True)
            if job_id in self._stopped:
                break

            counters = self._counters(job_id)
            self._set_job(job_id, cells_done=counters["cells_done"],
                          leads_found=counters["leads_found"])

        status = "done"
        if job_id in self._stopped:
            status = "stopped"
        counters = self._counters(job_id)
        self._set_job(job_id, status=status)
        self._emit(self._job_event(job_id, status))

    def _current_index(self, job_id: str) -> int:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT query_index FROM scan_job WHERE id = ?", (job_id,)
            ).fetchone()
        return int(row["query_index"] or 0) if row else 0

    def _job_event(self, job_id: str, status: str | None = None) -> dict:
        """Событие прогресса с текущей категорией и счётчиками."""
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM scan_job WHERE id = ?", (job_id,)).fetchone()
        queries = self._job_queries(row) if row else []
        index = 0
        if row and "query_index" in row.keys():
            index = int(row["query_index"] or 0)
        if queries:
            index = max(0, min(index, len(queries) - 1))
        return {
            "type": "job", "job_id": job_id,
            "status": status or (row["status"] if row else "running"),
            "queries_total": len(queries),
            "query_index": index,
            "current_query": queries[index] if queries else "",
            **self._counters(job_id),
        }

    def _reset_cells(self, job_id: str) -> None:
        """Сбросить сетку перед следующей категорией."""
        with db.connect() as conn:
            conn.execute(
                "UPDATE scan_cell SET status='pending', found=0, error='', "
                "started_at=NULL, finished_at=NULL WHERE job_id=?",
                (job_id,),
            )
            conn.commit()

    def _counters(self, job_id: str) -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE status='done') AS done "
                "FROM scan_cell WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            total = conn.execute(
                "SELECT cells_total FROM scan_job WHERE id = ?", (job_id,)
            ).fetchone()
        return {"cells_done": row["done"], "cells_total": total["cells_total"] if total else 0,
                "leads_found": self._new_counts.get(job_id, 0)}

    async def _run_global(self, job_id: str, adapter, query: str, bbox: BBox) -> None:
        self._emit({"type": "cell", "job_id": job_id, "ix": -1, "iy": -1,
                    "status": "running", "found": 0})
        try:
            places = await asyncio.wait_for(adapter.search(query, bbox, 500),
                                            timeout=GLOBAL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            log.warning("adapter %s failed: %s", adapter.name, exc)
            self._emit({"type": "error", "job_id": job_id,
                        "source": adapter.name, "message": str(exc)})
            self._emit({"type": "cell", "job_id": job_id, "ix": -1, "iy": -1,
                        "status": "error", "found": 0})
            return
        for place in places:
            self._store(job_id, place)
        counters = self._counters(job_id)
        self._set_job(job_id, leads_found=counters["leads_found"])
        self._emit({"type": "cell", "job_id": job_id, "ix": -1, "iy": -1,
                    "status": "done", "found": len(places)})
        self._emit(self._job_event(job_id, "running"))

    async def _scan_cell(self, job_id: str, cell: dict, query: str,
                         adapters: list, limit: int) -> None:
        cell_bbox: BBox = tuple(json.loads(cell["bbox_json"]))
        with db.connect() as conn:
            conn.execute(
                "UPDATE scan_cell SET status='running', started_at=? WHERE id=?",
                (db.now(), cell["id"]),
            )
            conn.commit()
        self._emit({"type": "cell", "job_id": job_id, "ix": cell["ix"],
                    "iy": cell["iy"], "status": "running", "found": 0})

        found = 0
        errors: list[str] = []

        async def call(adapter):
            return await asyncio.wait_for(adapter.search(query, cell_bbox, limit),
                                          timeout=CELL_TIMEOUT)

        results = await asyncio.gather(*(call(a) for a in adapters), return_exceptions=True)
        for adapter, result in zip(adapters, results):
            if isinstance(result, BaseException):
                errors.append(f"{adapter.name}: {result}")
                log.warning("cell %s/%s adapter %s: %s", cell["ix"], cell["iy"],
                            adapter.name, result)
                self._emit({"type": "error", "job_id": job_id,
                            "source": adapter.name, "message": str(result)})
                continue
            for place in result:
                self._store(job_id, place)
                found += 1

        with db.connect() as conn:
            conn.execute(
                "UPDATE scan_cell SET status='done', found=?, error=?, finished_at=? WHERE id=?",
                (found, "; ".join(errors)[:500], db.now(), cell["id"]),
            )
            conn.commit()

        counters = self._counters(job_id)
        self._set_job(job_id, cells_done=counters["cells_done"], leads_found=counters["leads_found"])
        self._emit({"type": "cell", "job_id": job_id, "ix": cell["ix"], "iy": cell["iy"],
                    "status": "done", "found": found, "error": "; ".join(errors)[:200]})
        self._emit(self._job_event(job_id, "running"))

    def _store(self, job_id: str, place: Place) -> None:
        if not place.name:
            return
        with db.connect() as conn:
            bid, is_new = db.upsert_place(conn, place)
            full = db.get_lead(conn, bid) or {
                "id": bid, "name": place.name, "source": place.source,
                "status": "new", "is_favorite": 0, "is_demo": 0,
            }
        if is_new:
            self._new_counts[job_id] = self._new_counts.get(job_id, 0) + 1
        self._emit({"type": "lead", "lead": full, "is_new": is_new, "job_id": job_id})


SCANNER = ScanManager()


def reset_stale_jobs() -> None:
    """При старте помечаем зависшие задания остановленными."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE scan_job SET status='stopped', updated_at=? "
            "WHERE status IN ('running','queued','paused')", (db.now(),)
        )
        conn.execute("UPDATE scan_cell SET status='pending' WHERE status='running'")
        conn.commit()
