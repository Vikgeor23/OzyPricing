import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from app.config import get_settings
from app.scrapers.sites.technopolis_occ_api import extract_product_code

e = create_engine(get_settings().database_url)
with e.connect() as c:
    rows = c.execute(
        text(
            """
            SELECT url FROM competitor_products
            WHERE url LIKE '%technopolis%' AND url LIKE '%/p/%'
            ORDER BY created_at DESC LIMIT 15
            """
        )
    ).fetchall()
    bad = 0
    for (u,) in rows:
        code = extract_product_code(u)
        ok = code and len(code) >= 4
        if not ok:
            bad += 1
        print(f"code={code!r} ok={ok}")
        print(f"  {u}\n")
