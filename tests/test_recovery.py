"""Recovery proves readable data and usable secrets after a clean restore."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from scripts.recovery import create, restore, verify


def test_bundle_round_trip_and_integrity_guards(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRGIL_ENCRYPTION_KEY", raising=False)
    source = tmp_path / "source"
    (source / "users").mkdir(parents=True)
    name = "00000000-0000-0000-0000-000000000001.db"
    key = Fernet.generate_key()
    secret = Fernet(key).encrypt(b"synthetic provider credential").decode()
    (source / "virgil.key").write_bytes(key)
    with sqlite3.connect(source / "central.db") as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, db_filename TEXT)")
        db.execute("INSERT INTO users VALUES ('one', ?)", (name,))
    with sqlite3.connect(source / "users" / name) as db:
        db.execute("CREATE TABLE llm_providers (id INTEGER PRIMARY KEY, api_key_enc TEXT)")
        db.execute("INSERT INTO llm_providers VALUES (1, ?)", (secret,))
    bundle = tmp_path / "bundle"
    create(source / "central.db", bundle)
    assert len(verify(bundle)["files"]) == 3
    destination = tmp_path / "restored"
    restore(bundle, destination)
    with sqlite3.connect(destination / "users" / name) as db:
        restored = db.execute("SELECT api_key_enc FROM llm_providers WHERE id=1").fetchone()[0]
    assert Fernet((destination / "virgil.key").read_bytes()).decrypt(restored) == b"synthetic provider credential"
    assert (destination / "virgil.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        restore(bundle, destination)
    (bundle / "users" / name).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        restore(bundle, tmp_path / "refused")
    assert not (tmp_path / "refused").exists()


def test_wrong_encryption_key_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRGIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with sqlite3.connect(tmp_path / "central.db") as db:
        db.execute("CREATE TABLE users (id TEXT, db_filename TEXT, totp_secret TEXT)")
        db.execute(
            "INSERT INTO users VALUES ('one', '00000000-0000-0000-0000-000000000001.db', ?)",
            (Fernet(Fernet.generate_key()).encrypt(b"secret").decode(),),
        )
    (tmp_path / "users").mkdir()
    with sqlite3.connect(tmp_path / "users/00000000-0000-0000-0000-000000000001.db") as db:
        db.execute("CREATE TABLE records (id INTEGER)")
    with pytest.raises(ValueError, match="cannot decrypt"):
        create(tmp_path / "central.db", tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_missing_user_database_does_not_publish_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRGIL_ENCRYPTION_KEY", raising=False)
    (tmp_path / "virgil.key").write_bytes(Fernet.generate_key())
    with sqlite3.connect(tmp_path / "central.db") as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, db_filename TEXT)")
        db.execute("INSERT INTO users VALUES ('one', '00000000-0000-0000-0000-000000000001.db')")
    with pytest.raises(ValueError, match="missing"):
        create(tmp_path / "central.db", tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_manifest_rejects_paths_outside_bundle(tmp_path):
    import json

    (tmp_path / "manifest.json").write_text(json.dumps({"format": 1, "files": {"../secret": "x"}}))
    with pytest.raises(ValueError, match="path"):
        verify(tmp_path)
