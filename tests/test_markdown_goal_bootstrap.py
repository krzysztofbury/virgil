import asyncio

import aiosqlite
import pytest
from conftest import user_db_path


def test_current_cele_bootstrap_is_idempotent_and_imports_one_off_reps(auth_client, tmp_path, monkeypatch):
    content = """# CELE

## CIAŁO - REGULARNOŚĆ I SIŁA
**Okno:** 03.08-31.12.2026

## KONTO - TEST SENSOWNEJ PRACY
**Okno:** 02.08-30.09.2026
1. Wysłać aplikację do 09.08.
2. Uruchomić cienki wycinek do 30.09.
"""
    (tmp_path / "cele.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr("app.services.markdown_import.SECOND_BRAIN_PATH", str(tmp_path))

    async def scenario():
        from app.services.markdown_import import import_cele

        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            await import_cele(db)
            imported_goal = await db.execute_fetchall(
                "SELECT id FROM goals WHERE source = 'second_brain' AND source_ref = 'cele:current:konto'"
            )
            assert imported_goal
            await db.execute(
                "UPDATE goals SET content = 'Edited canonically in Virgil', status = 'completed' WHERE id = ?",
                (imported_goal[0]["id"],),
            )
            await db.commit()
            (tmp_path / "cele.md").write_text(
                content.replace("TEST SENSOWNEJ PRACY", "CHANGED MARKDOWN"), encoding="utf-8"
            )
            await import_cele(db)
            goals = await db.execute_fetchall(
                "SELECT id, content, status, start_date, end_date FROM goals "
                "WHERE source = 'second_brain' AND source_ref LIKE 'cele:current:%' ORDER BY source_ref"
            )
            reps = await db.execute_fetchall(
                "SELECT content, period, due_date, status FROM goal_reps WHERE source = 'second_brain' ORDER BY content"
            )
            result = [dict(row) for row in goals], [dict(row) for row in reps]
            await db.execute("DELETE FROM goal_reps WHERE source = 'second_brain'")
            await db.execute("DELETE FROM goals WHERE source = 'second_brain'")
            await db.execute("DELETE FROM app_settings WHERE key = 'cele_bootstrap_version'")
            await db.commit()
            return result
        finally:
            await db.close()

    goals, reps = asyncio.run(scenario())
    assert len(goals) == 2
    assert {goal["content"] for goal in goals} == {"REGULARNOŚĆ I SIŁA", "Edited canonically in Virgil"}
    assert next(goal for goal in goals if goal["content"] == "Edited canonically in Virgil")["status"] == "completed"
    body = next(goal for goal in goals if goal["content"] == "REGULARNOŚĆ I SIŁA")
    assert (body["start_date"], body["end_date"]) == ("2026-08-03", "2026-12-31")
    assert len(reps) == 2
    assert all(rep["period"] == "month" and rep["status"] == "pending" for rep in reps)
    assert {rep["due_date"] for rep in reps} == {"2026-08-09", "2026-09-30"}


def test_current_cele_resolves_yearless_deadlines_across_new_year(auth_client, tmp_path, monkeypatch):
    (tmp_path / "cele.md").write_text(
        """# CELE

## KONTO - CROSS YEAR GOAL
**Okno:** 01.12.2026-31.01.2027
1. December action do 15.12.
2. January action do 15.01.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.markdown_import.SECOND_BRAIN_PATH", str(tmp_path))

    async def scenario():
        from app.services.markdown_import import import_cele

        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("DELETE FROM app_settings WHERE key = 'cele_bootstrap_version'")
            await import_cele(db)
            reps = await db.execute_fetchall(
                "SELECT due_date FROM goal_reps WHERE source = 'second_brain' ORDER BY due_date"
            )
            result = [row["due_date"] for row in reps]
            await db.execute("DELETE FROM goal_reps WHERE source = 'second_brain'")
            await db.execute("DELETE FROM goals WHERE source = 'second_brain'")
            await db.execute("DELETE FROM app_settings WHERE key = 'cele_bootstrap_version'")
            await db.commit()
            return result
        finally:
            await db.close()

    assert asyncio.run(scenario()) == ["2026-12-15", "2027-01-15"]


def test_import_all_rolls_back_every_section_when_the_final_import_fails(auth_client, monkeypatch):
    import app.services.markdown_import as markdown_import

    async def first_import(db):
        await db.execute("INSERT INTO app_settings(key, value) VALUES('atomic_import_probe', 'written')")

    async def noop_import(db):
        return None

    async def final_import_fails(db):
        raise RuntimeError("final section failed")

    monkeypatch.setattr(markdown_import, "import_badania", first_import)
    monkeypatch.setattr(markdown_import, "import_oura", noop_import)
    monkeypatch.setattr(markdown_import, "import_liczby", noop_import)
    monkeypatch.setattr(markdown_import, "import_noporn", noop_import)
    monkeypatch.setattr(markdown_import, "import_snapply", noop_import)
    monkeypatch.setattr(markdown_import, "import_cele", final_import_fails)

    async def scenario():
        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            with pytest.raises(RuntimeError, match="final section failed"):
                await markdown_import.import_all(db)
            rows = await db.execute_fetchall("SELECT value FROM app_settings WHERE key = 'atomic_import_probe'")
            return rows
        finally:
            await db.close()

    assert asyncio.run(scenario()) == []
