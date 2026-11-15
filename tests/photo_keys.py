"""Показать ключи дедупликации фото: python tests/photo_keys.py [db]"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db  # noqa: E402
from backend.enrichment import _image_key  # noqa: E402

conn = db.connect()
rows = conn.execute(
    "SELECT value FROM fact WHERE kind = 'photo' ORDER BY value"
).fetchall()
print("photo facts:", len(rows))
for index, row in enumerate(rows, 1):
    value = row["value"]
    print(index, "KEY:", _image_key(value)[:240])
    print("   URL:", value[:240])