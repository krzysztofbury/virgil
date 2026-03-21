"""CLI script for importing data from markdown files."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import close_db, get_db, init_db
from app.services.markdown_import import import_all


async def main():
    await init_db()
    db = await get_db()
    print("Starting import...")
    await import_all(db)
    print("Import complete.")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
