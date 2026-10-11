"""Import markdown into an explicitly selected, existing account."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.central_db import close_central_db, get_user_by_email
from app.config import CENTRAL_DB_PATH, SECOND_BRAIN_PATH, USERS_DB_DIR
from app.services.markdown_import import import_all
from app.user_db import close_user_db, open_user_db


async def import_account(email: str) -> None:
    if not Path(CENTRAL_DB_PATH).is_file():
        raise ValueError("Central registry does not exist; start Virgil and create the account first")
    if not SECOND_BRAIN_PATH or not Path(SECOND_BRAIN_PATH).is_dir():
        raise ValueError("Set VIRGIL_SECOND_BRAIN_PATH to an existing markdown directory")
    try:
        user = await get_user_by_email(email.strip().lower())
        if not user or not user["is_active"]:
            raise ValueError("An active account with that email does not exist")
        if not (Path(USERS_DB_DIR) / user["db_filename"]).is_file():
            raise ValueError("The account database is missing; restore it before importing")
        db = await open_user_db(user["db_filename"])
        try:
            await import_all(db)
        finally:
            await close_user_db(db)
    finally:
        await close_central_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Existing account receiving the import")
    args = parser.parse_args()
    try:
        asyncio.run(import_account(args.email))
    except ValueError as exc:
        parser.exit(2, f"Import refused: {exc}\n")
    print("Import complete.")


if __name__ == "__main__":
    main()
