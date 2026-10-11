"""The import CLI must resolve a registered account, never create a legacy DB."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_cli_refuses_missing_registry_and_requires_account(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/import_data.py"
    env = {
        **os.environ,
        "VIRGIL_CENTRAL_DB_PATH": str(tmp_path / "central.db"),
        "VIRGIL_SECOND_BRAIN_PATH": str(tmp_path),
    }
    result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "--email" in result.stderr
    result = subprocess.run(
        [sys.executable, str(script), "--email", "absent@example.com"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Central registry does not exist" in result.stderr
    assert not (tmp_path / "central.db").exists()


def test_import_targets_only_selected_account(auth_client, tmp_path):
    from conftest import user_db_path

    central = tmp_path / "central.db"
    users = tmp_path / "users"
    users.mkdir()
    with sqlite3.connect(central) as db:
        db.execute("CREATE TABLE users (email TEXT, db_filename TEXT, is_active INTEGER)")
        db.executemany(
            "INSERT INTO users VALUES (?, ?, 1)", [("one@example.com", "one.db"), ("two@example.com", "two.db")]
        )
    for name in ("one.db", "two.db"):
        source = sqlite3.connect(user_db_path())
        destination = sqlite3.connect(users / name)
        try:
            source.backup(destination)
            destination.execute("DELETE FROM daily_logs WHERE date = '2030-01-01'")
            destination.commit()
        finally:
            source.close()
            destination.close()
    (tmp_path / "liczby.md").write_text("# 2030\n### 01.01 (Tuesday)\n- Energy: 7/10\n")
    script = Path(__file__).resolve().parents[1] / "scripts/import_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--email", "TWO@example.com"],
        env={**os.environ, "VIRGIL_CENTRAL_DB_PATH": str(central), "VIRGIL_SECOND_BRAIN_PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for name, expected in (("one.db", []), ("two.db", [(7,)])):
        with sqlite3.connect(users / name) as db:
            assert db.execute("SELECT energy FROM daily_logs WHERE date = '2030-01-01'").fetchall() == expected
