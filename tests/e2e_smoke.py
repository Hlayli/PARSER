"""Интеграционная проверка HTTP-API на живом сервере (демо-источник).

Запуск:
    set PARSER_DEMO=1
    python -m uvicorn backend.app:app --port 8791
    python tests/e2e_smoke.py
"""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8791"
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("OK   " if condition else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def wait_health(seconds: float = 25) -> dict:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{BASE}/api/health", timeout=5)
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise SystemExit(f"сервер не поднялся: {last}")


def main() -> None:
    health = wait_health()
    check("health 200", health.get("ok") is True)
    names = {a["name"] for a in health.get("adapters", [])}
    check("адаптеры перечислены", {"google", "yandex", "2gis", "demo"} <= names,
          str(sorted(names)))

    client = httpx.Client(base_url=BASE, timeout=30)
    stream_client = httpx.Client(base_url=BASE, timeout=30)

    # --- обычный скан -------------------------------------------------------
    response = client.post("/api/scan", json={
        "query": "кофейни",
        "bbox": [65.50, 57.10, 65.52, 57.12],
        "cell_deg": 0.05,
        "sources": ["demo"],
    })
    check("POST /api/scan", response.status_code == 200, f"HTTP {response.status_code}")
    scan = response.json()
    job_id = scan.get("job_id", "")
    check("job_id получен", bool(job_id), job_id)

    # --- ждём завершения первого задания, чтобы события не смешивались -------
    final = None
    for _ in range(60):
        response = client.get(f"/api/jobs/{job_id}")
        if response.status_code != 200:
            break
        final = response.json()
        if final.get("status") in ("done", "stopped", "error"):
            break
        time.sleep(0.5)
    check("задание завершилось", bool(final and final.get("status") == "done"),
          str(final and final.get("status")))
    check("ячейки выполнены",
          bool(final and final["cells_done"] == final["cells_total"]),
          str(final and (final["cells_done"], final["cells_total"])))

    # --- SSE: подписка, затем второй скан ----------------------------------
    with stream_client.stream("GET", "/api/events") as stream:
        check("SSE content-type",
              "text/event-stream" in stream.headers.get("content-type", ""),
              stream.headers.get("content-type", ""))
        lines = stream.iter_lines()

        first = next(lines, "")
        check("SSE hello", first.startswith("data: ") and
              json.loads(first[6:]).get("type") == "hello", first)

        sse_scan = client.post("/api/scan", json={
            "query": "пекарни",
            "bbox": [65.60, 57.20, 65.62, 57.22],
            "cell_deg": 0.05,
            "sources": ["demo"],
        }).json()
        sse_job_id = sse_scan.get("job_id", "")
        check("SSE-скан запущен", bool(sse_job_id), str(sse_scan))

        events = []
        deadline = time.time() + 25
        while time.time() < deadline:
            line = next(lines, "")
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event.get("job_id") not in (None, sse_job_id):
                    continue
                events.append(event)
                if event.get("type") == "job" and event.get("status") == "done":
                    break
        types = {e.get("type") for e in events}
        counts = {t: sum(1 for e in events if e.get("type") == t) for t in types}
        check("SSE lead", "lead" in types, str(counts))
        check("SSE cell", "cell" in types, str(counts))
        check("SSE job done", any(e.get("type") == "job" and e.get("status") == "done"
                                  for e in events), str(counts))
        check("SSE 10 лидов", counts.get("lead", 0) >= 10, str(counts))

    # --- лиды, карточка, статусы -------------------------------------------
    leads = client.get("/api/leads", params={"limit": 100}).json()
    check("лиды найдены", leads.get("total", 0) >= 10, str(leads.get("total")))
    check("все лиды помечены ДЕМО",
          all(i.get("is_demo") == 1 for i in leads.get("items", [])))
    check("имена с префиксом ДЕМО",
          all(i["name"].startswith("ДЕМО") for i in leads.get("items", [])))
    check("бейджи источников", all(i.get("sources") for i in leads.get("items", [])))

    first_lead = leads["items"][0]
    detail = client.get(f"/api/leads/{first_lead['id']}").json()
    check("карточка лида", detail.get("name") == first_lead["name"])
    check("есть source_links", isinstance(detail.get("source_links"), list))

    updated = client.patch(f"/api/leads/{first_lead['id']}", json={
        "status": "called", "is_favorite": True, "note": "проверка",
    }).json()
    check("PATCH статуса",
          updated["lead"]["status"] == "called"
          and updated["lead"]["is_favorite"] == 1
          and updated["lead"]["note"] == "проверка")

    check("невалидный статус → 422",
          client.patch(f"/api/leads/{first_lead['id']}",
                       json={"status": "нечто"}).status_code in (400, 422))
    check("PATCH 404",
          client.patch("/api/leads/no-such-id",
                       json={"status": "called"}).status_code == 404)

    favs = client.get("/api/leads", params={"fav": "true"}).json()
    check("фильтр избранных", any(i["id"] == first_lead["id"] for i in favs["items"]))

    stats = client.get("/api/stats").json()
    check("stats", stats.get("total", 0) >= 10, str(stats.get("total")))
    check("stats favorites", stats.get("favorites", 0) >= 1, str(stats.get("favorites")))

    # --- экспорт и статика ---------------------------------------------------
    export = client.get("/api/export.csv")
    text = export.content.decode("utf-8-sig")
    header = text.splitlines()[0] if text else ""
    check("экспорт CSV", export.status_code == 200 and "Название" in header,
          f"HTTP {export.status_code}, {len(text)} байт")
    check("CSV содержит лиды", "ДЕМО" in text)

    index = client.get("/")
    check("главная страница", index.status_code == 200 and b"PARSER" in index.content,
          f"HTTP {index.status_code}")
    app_js = client.get("/static/app.js")
    check("статика app.js", app_js.status_code == 200 and b"EventSource" in app_js.content,
          f"HTTP {app_js.status_code}")

    # --- валидация ----------------------------------------------------------
    check("битый bbox → 400",
          client.post("/api/scan", json={
              "query": "x", "bbox": [1, 2, 3], "sources": ["demo"]}).status_code == 400)
    check("без города/bbox → 400",
          client.post("/api/scan", json={
              "query": "x", "city": "", "sources": ["demo"]}).status_code == 400)

    client.close()
    stream_client.close()


if __name__ == "__main__":
    main()
    print()
    if FAILED:
        print("ПРОВАЛЕНО:", ", ".join(FAILED))
        sys.exit(1)
    print("Все проверки пройдены.")