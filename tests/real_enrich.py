"""Проверка обогащения на реальном сайте: python tests/real_enrich.py <url>"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db  # noqa: E402
from backend.enrichment import enrich  # noqa: E402
from backend.models import Place  # noqa: E402


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://gardencoffee.ru/"
    db.init_db()
    with db.connect() as conn:
        business_id, _ = db.upsert_place(conn, Place(
            source="google",
            source_id=f"probe-{abs(hash(url)) % 10**8}",
            name="Проверка обогащения",
            website=url,
            lat=57.11,
            lon=65.51,
        ))

    result = await enrich(business_id)
    print("added:", result["added"])
    print("errors:", result["errors"][:5])

    with db.connect() as conn:
        lead = db.get_lead(conn, business_id)
    for kind, facts in (lead.get("facts") or {}).items():
        print(f"{kind}:")
        for fact in facts[:3]:
            print("   ", fact["value"][:140], "|", fact["source_url"][:100])
    print("photos:", len(lead.get("photos") or []))
    for photo in (lead.get("photos") or [])[:5]:
        print("   ", photo[:140])


asyncio.run(main())