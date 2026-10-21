"""Complete recovery bundles: every database, the encryption key and a checksum manifest.

scripts/recovery.py is the operator's CLI for these; the off-site job builds
the same bundle while the application runs. Restore only to a new data
directory, then point the stopped application at it. Existing data is never replaced.
"""

import gzip
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
from pyrage import DecryptError, decrypt_file, encrypt_file, x25519

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


# An off-site copy is a bundle with each file stored separately, compressed and
# then encrypted, so one account's file can be deleted or restored on its own.
SEALED_SUFFIX = ".gz.age"
# Far past any real database here, short of a disk filled by a crafted file.
UNSEALED_MAX_BYTES = 16 * 1024**3


def parse_recipient(text: str) -> x25519.Recipient:
    """The age public key off-site copies are encrypted to, or ValueError."""
    try:
        return x25519.Recipient.from_str(text.strip())
    except Exception:
        raise ValueError("Not an age public key (age1...)") from None


def read_identities(identity_file: Path) -> list[x25519.Identity]:
    """The secret keys in an age identity file, as age-keygen writes it."""
    lines = [line.strip() for line in identity_file.read_text().splitlines()]
    try:
        identities = [x25519.Identity.from_str(line) for line in lines if line and not line.startswith("#")]
    except Exception:
        raise ValueError("The identity file holds something other than age secret keys") from None
    if not identities:
        raise ValueError("The identity file holds no age secret key")
    return identities


def seal(source: Path, sealed: Path, recipient: x25519.Recipient) -> None:
    """Compress then encrypt one bundle file. `age -d -i identity.txt F | gunzip` opens it too."""
    with tempfile.TemporaryDirectory(prefix=".virgil-seal-", dir=sealed.parent) as temporary:
        compressed = Path(temporary) / "file.gz"
        with source.open("rb") as raw, gzip.open(compressed, "wb") as packed:
            shutil.copyfileobj(raw, packed)
        encrypt_file(str(compressed), str(sealed), [recipient])


def unseal(directory: Path, identity_file: Path, destination: Path) -> dict:
    """Decrypt a downloaded off-site copy into a new, verified bundle directory.

    `directory` holds the copy's files as stored, for example
    manifest.json.gz.age and users/<id>.db.gz.age.
    """
    if destination.exists():
        raise FileExistsError("Destination already exists")
    identities = read_identities(identity_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".virgil-unseal-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir(mode=0o700)
        (staging / "users").mkdir(mode=0o700)
        for sealed in directory.rglob(f"*{SEALED_SUFFIX}"):
            name = str(sealed.relative_to(directory)).removesuffix(SEALED_SUFFIX)
            if name not in {CENTRAL_NAME, "virgil.key", "manifest.json"} and not (
                name.startswith("users/") and USER_FILE.fullmatch(name.removeprefix("users/"))
            ):
                raise ValueError("Unexpected file in the off-site copy")
            compressed = Path(temporary) / "file.gz"
            try:
                decrypt_file(str(sealed), str(compressed), identities)
            except DecryptError:
                raise ValueError("A file does not decrypt with this identity, or it is damaged") from None
            with gzip.open(compressed, "rb") as packed, (staging / name).open("wb") as raw:
                written = 0
                while chunk := packed.read(1024 * 1024):
                    written += len(chunk)
                    if written > UNSEALED_MAX_BYTES:
                        raise ValueError("A file in the off-site copy is larger than any database should be")
                    raw.write(chunk)
            (staging / name).chmod(0o600)
            compressed.unlink()
        manifest = verify(staging)
        staging.rename(destination)
    return manifest
