"""Полный скан тремя реальными источниками через живой сервер.

Сервер должен быть запущен на 8791 (демо-режим не мешает: в запросе только
google, yandex, 2gis). Город обязателен для 2ГИС.
"""

import time

import httpx

BASE = "http://127.0.0.1:8791"
BBOX = [65.50, 57.10, 65.52, 57.12]

client = httpx.Client(base_url=BASE, timeout=60)

scan = client.post("/api/scan", json={
    "query": "кофейни",
    "city": "Тюмень",
    "bbox": BBOX,
    "cell_deg": 0.02,
    "sources": ["google", "yandex", "2gis"],
})
print("scan:", scan.status_code, scan.text[:300])
scan.raise_for_status()
job_id = scan.json()["job_id"]

started = time.monotonic()
final = None
while time.monotonic() - started < 240:
    response = client.get(f"/api/jobs/{job_id}")
    final = response.json()
    print(
        f"  {time.monotonic() - started:5.1f}s "
        f"status={final['status']} cells={final['cells_done']}/{final['cells_total']} "
        f"leads={final['leads_found']} error={final['error'][:80]!r}"
    )
    if final["status"] in ("done", "stopped", "error"):
        break
    time.sleep(5)

print("итог:", final["status"], "за", round(time.monotonic() - started, 1), "с")

leads = client.get("/api/leads", params={"limit": 100}).json()
by_source = {}
for item in leads.get("items", []):
    for source in item.get("sources", []):
        by_source[source] = by_source.get(source, 0) + 1
print("лидов:", leads.get("total"), "по источникам:", by_source)
for item in leads.get("items", [])[:10]:
    print(f"- {item['name']} | {item['sources']} | {item['phone']} | {item['website']}")

client.close()