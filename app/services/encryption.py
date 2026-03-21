import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import CENTRAL_DB_PATH, ENCRYPTION_KEY

_fernet: Fernet | None = None
_raw_key: str | None = None


def _key_file_path() -> Path:
    # Key lives alongside the central DB (in /data/ for Docker, ./data/ locally).
    return Path(CENTRAL_DB_PATH).parent / "virgil.key"


def _load_raw_key() -> str:
    """Load or generate the raw encryption key string. Cached in module global."""
    global _raw_key
    if _raw_key is not None:
        return _raw_key

    key = ENCRYPTION_KEY
    if not key:
        key_file = _key_file_path()
        if key_file.exists():
            key = key_file.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            key_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)

    _raw_key = key
    return _raw_key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = _load_raw_key()
    # Use explicit raises — these guard cryptographic material and must survive python -O.
    if not key:
        raise ValueError("Encryption key is empty")
    if len(key) != 44:
        raise ValueError(f"Encryption key has unexpected length {len(key)}, expected 44")
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def get_signing_key() -> str:
    """Derive a stable signing key from the encryption key for session cookies."""
    raw = _load_raw_key()
    return hashlib.sha256(raw.encode()).hexdigest()


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return get_fernet().decrypt(ciphertext.encode()).decode()
