"""Create, verify, or restore a complete offline recovery bundle.

Stop every Virgil process before creating a bundle. Restore only to a new data
directory, then point the stopped application at it. Existing data is never replaced.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

USER_FILE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.db\Z")
CENTRAL_NAME = "virgil-central.db"


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check_database(path: Path) -> None:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign-key check failed")


def user_files(central: Path) -> list[str]:
    with closing(sqlite3.connect(central.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        names = [row[0] for row in db.execute("SELECT db_filename FROM users ORDER BY id")]
    if len(names) > 10000 or len(set(names)) != len(names) or any(not USER_FILE.fullmatch(n) for n in names):
        raise ValueError("Invalid database references in central registry")
    return names


def copy_database(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("A required source database is missing or is a symlink")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    destination.chmod(0o600)
    check_database(destination)


def verify(bundle: Path) -> dict:
    if bundle.is_symlink():
        raise ValueError("Bundle must be a real directory")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 2_000_000:
        raise ValueError("Invalid recovery manifest")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported recovery format")
    files = manifest["files"]
    if len(files) > 10002:
        raise ValueError("Too many recovery files")
    for name, expected in files.items():
        if name not in {CENTRAL_NAME, "virgil.key"} and not (
            name.startswith("users/") and USER_FILE.fullmatch(name.removeprefix("users/"))
        ):
            raise ValueError("Unexpected recovery path")
        path = bundle / name
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError("Recovery file missing or checksum mismatch")
        if name.endswith(".db"):
            check_database(path)
    names = user_files(bundle / CENTRAL_NAME)
    if set(files) != {CENTRAL_NAME, "virgil.key", *(f"users/{name}" for name in names)}:
        raise ValueError("Bundle does not match the central registry")
    cipher = Fernet((bundle / "virgil.key").read_bytes().strip())
    for name in files:
        if not name.endswith(".db"):
            continue
        with closing(sqlite3.connect((bundle / name).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for table, column in (
                ("users", "totp_secret"),
                ("llm_providers", "api_key_enc"),
                ("integrations", "client_secret_enc"),
                ("integrations", "access_token_enc"),
                ("integrations", "refresh_token_enc"),
            ):
                if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    continue
                for (value,) in db.execute(f"SELECT {column} FROM {table}"):
                    # Legacy MFA secrets can be unencrypted base32.
                    if not value or (column == "totp_secret" and not value.startswith("gAAAA")):
                        continue
                    try:
                        cipher.decrypt(value.encode())
                    except InvalidToken:
                        raise ValueError("Recovery key cannot decrypt stored credentials") from None
    return manifest


def create(central: Path, destination: Path, key_file: Path | None = None) -> None:
    if destination.exists():
        raise FileExistsError("Destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("VIRGIL_ENCRYPTION_KEY", "").encode()
    if key_file is not None:
        key = key_file.read_bytes().strip()
    elif not key:
        key = central.with_name("virgil.key").read_bytes().strip()
    Fernet(key)
    with tempfile.TemporaryDirectory(prefix=".virgil-recovery-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir(mode=0o700)
        (staging / "users").mkdir(mode=0o700)
        copy_database(central, staging / CENTRAL_NAME)
        for name in user_files(staging / CENTRAL_NAME):
            copy_database(central.parent / "users" / name, staging / "users" / name)
        (staging / "virgil.key").write_bytes(key)
        (staging / "virgil.key").chmod(0o600)
        files = {str(path.relative_to(staging)): digest(path) for path in staging.rglob("*") if path.is_file()}
        (staging / "manifest.json").write_text(
            json.dumps({"format": 1, "created_at": datetime.now(UTC).isoformat(), "files": files}, indent=2) + "\n"
        )
        (staging / "manifest.json").chmod(0o600)
        verify(staging)
        staging.rename(destination)


def restore(bundle: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError("Restore requires a new destination directory")
    manifest = verify(bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".virgil-restore-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "data"
        staging.mkdir(mode=0o700)
        (staging / "users").mkdir(mode=0o700)
        for name in manifest["files"]:
            shutil.copyfile(bundle / name, staging / name)
            (staging / name).chmod(0o600)
        # Recheck copied bytes before publishing, including source changes during copying.
        shutil.copyfile(bundle / "manifest.json", staging / "manifest.json")
        verify(staging)
        (staging / "manifest.json").unlink()
        staging.rename(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("--central", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--key-file", type=Path)
    backup.add_argument("--stopped", action="store_true", required=True, help="Confirm all Virgil writers are stopped")
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    recovery = commands.add_parser("restore")
    recovery.add_argument("bundle", type=Path)
    recovery.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            create(args.central, args.destination, args.key_file)
        elif args.command == "restore":
            restore(args.bundle, args.destination)
        else:
            verify(args.bundle)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"Recovery refused: {exc}\n")
    print(f"Recovery {args.command} complete.")


if __name__ == "__main__":
    main()
