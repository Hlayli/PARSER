"""Точечная проверка SSE: подписка, затем запуск скана, чтение всех строк."""

import asyncio
import json
import time

import httpx

BASE = "http://127.0.0.1:8791"


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=30.0)) as client:
        async with client.stream("GET", f"{BASE}/api/events") as resp:
            print("status", resp.status_code, resp.headers.get("content-type"))
            lines = resp.aiter_lines()
            first = await anext(lines)
            print("first:", first)

            scan = await client.post(f"{BASE}/api/scan", json={
                "query": "столовые",
                "bbox": [65.70, 57.30, 65.72, 57.32],
                "cell_deg": 0.05,
                "sources": ["demo"],
            })
            print("scan:", scan.status_code, scan.json())

            seen = []
            deadline = time.time() + 12
            while time.time() < deadline:
                try:
                    line = await asyncio.wait_for(anext(lines), timeout=deadline - time.time())
                except (asyncio.TimeoutError, StopAsyncIteration):
                    print("stream closed/timeout")
                    break
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    seen.append(event)
                    print("event:", event.get("type"), event.get("status", ""),
                          event.get("ix", ""), (event.get("lead") or {}).get("name", ""))
                    if event.get("type") == "job" and event.get("status") == "done":
                        break

            counts = {}
            for event in seen:
                counts[event.get("type")] = counts.get(event.get("type"), 0) + 1
            print("counts:", counts)


asyncio.run(main())
