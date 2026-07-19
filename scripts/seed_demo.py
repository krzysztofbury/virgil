"""Seed a self-contained DEMO account with realistic but entirely fictional data.

Use it to take clean screenshots for the README/docs without exposing real
personal data. Idempotent: re-running wipes and recreates the demo user.

    uv run python scripts/seed_demo.py

Then run the app and log in with the credentials printed at the end.
The generated data lives in the (gitignored) data/ dir — never committed.
"""

import asyncio
import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.central_db import (  # noqa: E402
    close_central_db,
    create_user,
    delete_user,
    get_user_by_email,
    init_central_db,
)
from app.user_db import close_user_db, create_user_db, delete_user_db, open_user_db  # noqa: E402

DEMO_EMAIL = "demo@virgil.app"
DEMO_PASSWORD = "demo-password-123"
TODAY = date.today()


def d(offset: int) -> str:
    return (TODAY - timedelta(days=offset)).isoformat()


async def _seed(db) -> None:
    # --- account state: skip onboarding, enable the No Porn module ---
    await db.executemany(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        [("onboarding_completed", "1"), ("feature_no_porn", "1")],
    )

    # The default seed lists some exercises twice — dedup for clean demo screenshots.
    await db.execute(
        "DELETE FROM training_exercises WHERE id NOT IN (SELECT MIN(id) FROM training_exercises GROUP BY section, name)"
    )
    await db.execute(
        "DELETE FROM exercise_library WHERE id NOT IN (SELECT MIN(id) FROM exercise_library GROUP BY section, name)"
    )

    # --- Oura: 45 days of daily data (smooth waves so charts look alive) ---
    oura = []
    for i in range(45):
        day = d(44 - i)
        wave = math.sin(i / 5)
        oura.append(
            (
                day,
                round(78 + 8 * wave),  # sleep_score
                round(76 + 7 * math.sin(i / 6 + 1)),  # readiness
                round(80 + 6 * math.sin(i / 4)),  # activity
                round(8500 + 3000 * math.sin(i / 3)),  # steps
                round(7.0 + 0.9 * wave, 2),  # sleep hours
                round(1.3 + 0.3 * wave, 2),  # deep
                round(1.5 + 0.4 * math.sin(i / 4), 2),  # rem
                round(54 - 3 * wave),  # resting_hr
                round(50 - 2 * wave),  # lowest_hr
                round(45 + 15 * math.sin(i / 5)),  # avg_hrv
            )
        )
    await db.executemany(
        "INSERT INTO oura_daily (date, sleep_score, readiness_score, activity_score, steps, "
        "sleep_duration_hours, deep_sleep_hours, rem_sleep_hours, resting_hr, lowest_hr, avg_hrv) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        oura,
    )

    # --- Oura monthly: 10 months back (feeds the Monthly Trends chart) ---
    months = []
    for i in range(10):
        m = TODAY.month - i
        y = TODAY.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        w = math.sin(i / 3)
        months.append(
            (
                f"{y:04d}-{m:02d}",
                round(80 + 5 * w),
                round(78 + 5 * math.sin(i / 4)),
                round(79 + 4 * w),
                round(9000 + 1500 * w),
                round(7.1 + 0.5 * w, 2),
                round(1.3 + 0.2 * w, 2),
                round(1.5 + 0.3 * w, 2),
                round(53 - 2 * w),
                round(49 - 2 * w),
                round(46 + 10 * w),
            )
        )
    await db.executemany(
        "INSERT INTO oura_monthly (month, sleep_score, readiness, activity, steps, "
        "sleep_duration, deep_sleep, rem_sleep, rhr, lowest_hr, hrv) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        months,
    )

    # --- Daily logs: last 14 days (generic, non-personal A.N.D.Y. tasks) ---
    body = ["30-min kettlebell session", "Zone-2 walk outside", "Full-body strength", "Mobility + stretch"]
    spirit = ["10-min meditation", "Journal 3 gratitudes", "Read 15 pages", "Breathwork before bed"]
    account = ["Ship one small feature", "Draft a blog post", "Study 30 min", "Inbox to zero"]
    relations = ["Call a friend", "Plan a weekend outing", "Message family", "Coffee with a colleague"]
    st = ["done", "done", "pending", "skipped"]
    logs = []
    for i in range(14):
        day = d(13 - i)
        logs.append(
            (
                day,
                5 + (i % 5),
                st[i % 4],
                st[(i + 1) % 4],
                st[(i + 2) % 4],
                "done",
                body[i % 4],
                "done",
                spirit[i % 4],
                "pending",
                account[i % 4],
                "skipped",
                relations[i % 4],
                "",
            )
        )
    await db.executemany(
        "INSERT INTO daily_logs (date, energy, morning_routine, evening_routine, water, "
        "andy_body_status, andy_body_desc, andy_spirit_status, andy_spirit_desc, "
        "andy_account_status, andy_account_desc, andy_relations_status, andy_relations_desc, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        logs,
    )

    # --- Body measurements (fictional, trending) ---
    await db.executemany(
        "INSERT INTO body_measurements (date, weight, arm, waist, hips, thighs) VALUES (?,?,?,?,?,?)",
        [
            (d(30), 84.0, 35.0, 88.0, 97.0, 58.0),
            (d(14), 83.0, 35.5, 87.0, 96.5, 58.5),
            (d(2), 82.0, 36.0, 86.0, 96.0, 59.0),
        ],
    )

    # --- No Porn (feniks): 90-day goal, one slip 12 days ago -> streak 12 ---
    await db.execute(
        "INSERT OR REPLACE INTO feniks_config (id, start_date, target_days, big_why) VALUES (1, ?, 90, ?)",
        (d(60), "Stay clear-headed and present."),
    )
    await db.execute(
        "INSERT INTO pmo_events (date, event_type, notes) VALUES (?, 'relapse', ?)",
        (d(12), "Tired and bored — noted the trigger."),
    )
    await db.executemany(
        "INSERT INTO feniks_journal (date, emotions, triggers, thoughts, desired_feelings, coping_strategies) "
        "VALUES (?,?,?,?,?,?)",
        [(d(1), "Calm, focused", "Evening downtime", "One day at a time", "Proud, rested", "Walk + early night")],
    )
    await db.executemany(
        "INSERT INTO feniks_pleasures (date, pleasure_1, pleasure_2) VALUES (?,?,?)",
        [(d(1), "Long walk in the sun", "Good coffee, no phone")],
    )

    # --- Training: 3 sessions this week over the ACTUAL seeded Core exercises ---
    # (names differ per install; dedup because the default seed can list one twice)
    core_all = await db.execute_fetchall(
        "SELECT id, name, metric FROM training_exercises WHERE section = 'Core' ORDER BY display_order"
    )
    seen: set[str] = set()
    core_ex = []
    for r in core_all:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        core_ex.append(r)
    core_ex = core_ex[:6]
    for sess_offset, minutes in ((0, 24), (2, 26), (4, 22)):
        cur = await db.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, ?, '')",
            (d(sess_offset), minutes),
        )
        sid = cur.lastrowid
        for ex in core_ex:
            for s in range(1, 5):  # 4 sets each
                if ex["metric"] == "time":
                    await db.execute(
                        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
                        "VALUES (?,?,?,NULL,?,?)",
                        (sid, ex["id"], s, 16.0, 40.0),
                    )
                else:
                    await db.execute(
                        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) "
                        "VALUES (?,?,?,?,?)",
                        (sid, ex["id"], s, 12, 20.0),
                    )

    # --- Life scores: two assessments for the radar comparison ---
    await db.executemany(
        "INSERT INTO life_scores (date, planning, spirituality, health, work, social, growth, relaxation, family, "
        "power_level) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(d(140), 3, 2, 4, 6, 3, 3, 2, 4, 3.0), (d(2), 8, 5, 6, 5, 4, 6, 8, 7, 7.0)],
    )

    # --- Experiment: active 4-week consistency sprint ---
    cur = await db.execute(
        "INSERT INTO experiments (title, description, start_date, num_weeks, status) VALUES (?,?,?,?,'active')",
        ("5/7 Consistency Sprint", "Hit the target on at least 5 of 7 days.", d(4), 4),
    )
    exp_id = cur.lastrowid
    await db.execute(
        "INSERT INTO experiment_weeks (experiment_id, week_number, label, target_min, target_max) VALUES (?,1,'',?,?)",
        (exp_id, 10, 14),
    )
    cur = await db.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, display_order) "
        "VALUES (?,?,?,'duration',0)",
        (exp_id, "Focus block", "#7c5cff"),
    )
    at_id = cur.lastrowid
    await db.executemany(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, value, notes, source) "
        "VALUES (?,?,?,?,?,'manual')",
        [(exp_id, d(1), at_id, 10, ""), (exp_id, d(0), at_id, 10, "")],
    )

    # --- Experiment 2: general (non-sport) protocol — boolean + count metrics ---
    cur = await db.execute(
        "INSERT INTO experiments (title, description, start_date, num_weeks, status) VALUES (?,?,?,?,'active')",
        (
            "Daily Reset Protocol",
            "Meditate daily; log each urge and whether the 10-min gate was executed. Success = 8 gates total.",
            d(9),
            2,
        ),
    )
    exp2_id = cur.lastrowid
    for wn in (1, 2):
        await db.execute(
            "INSERT INTO experiment_weeks (experiment_id, week_number, label, target_min, target_max) "
            "VALUES (?,?,'',0,0)",
            (exp2_id, wn),
        )
    cur = await db.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period, display_order) "
        "VALUES (?,?,?,'boolean',1,'day',1)",
        (exp2_id, "Meditation", "#22c55e"),
    )
    med_id = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period, display_order) "
        "VALUES (?,?,?,'count',0,'week',2)",
        (exp2_id, "Urge logged", "#ef4444"),
    )
    urge_id = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period, display_order) "
        "VALUES (?,?,?,'count',8,'total',3)",
        (exp2_id, "Gate executed", "#3b82f6"),
    )
    gate_id = cur.lastrowid
    await db.executemany(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, value, notes, source) "
        "VALUES (?,?,?,?,?,'manual')",
        [
            (exp2_id, d(8), med_id, 1, ""),
            (exp2_id, d(7), med_id, 1, ""),
            (exp2_id, d(6), med_id, 0, "skipped — travel"),
            (exp2_id, d(5), med_id, 1, ""),
            (exp2_id, d(4), med_id, 1, ""),
            (exp2_id, d(2), med_id, 1, ""),
            (exp2_id, d(1), med_id, 1, ""),
            (exp2_id, d(7), urge_id, 1, "TENSION, craving 7→3"),
            (exp2_id, d(7), gate_id, 1, "10-min walk"),
            (exp2_id, d(4), urge_id, 1, "AUTOPILOT, craving 5→2"),
            (exp2_id, d(4), gate_id, 1, "device out of room"),
            (exp2_id, d(1), urge_id, 1, "EMPTINESS, craving 6→4"),
        ],
    )

    # --- Bloodwork: a few markers + latest results (so the Blood page isn't empty) ---
    markers = [
        ("Testosterone", "Hormones", "ng/dL", 300.0, 1000.0, 0),
        ("Vitamin D", "Vitamins", "ng/mL", 30.0, 100.0, 1),
        ("Ferritin", "Iron", "ng/mL", 30.0, 400.0, 2),
        ("HbA1c", "Metabolic", "%", 4.0, 5.6, 3),
        ("LDL", "Lipids", "mg/dL", 0.0, 100.0, 4),
    ]
    for name, cat, unit, lo, hi, order in markers:
        cur = await db.execute(
            "INSERT INTO blood_markers (name, category, unit, ref_low, ref_high, display_order) VALUES (?,?,?,?,?,?)",
            (name, cat, unit, lo, hi, order),
        )
        mid = cur.lastrowid
        for off, val, flag in ((180, lo * 1.1, ""), (10, hi * 0.9, "")):
            await db.execute(
                "INSERT INTO blood_results (marker_id, date, value, flag) VALUES (?,?,?,?)",
                (mid, d(off), round(val, 1), flag),
            )

    await db.commit()


async def main() -> None:
    await init_central_db()
    try:
        existing = await get_user_by_email(DEMO_EMAIL)
        if existing:
            fname = await delete_user(existing["id"])
            if fname:
                delete_user_db(fname)
            print(f"Removed existing demo user ({DEMO_EMAIL})")

        user = await create_user(DEMO_EMAIL, DEMO_PASSWORD, "Demo User")
        await create_user_db(user["db_filename"])

        db = await open_user_db(user["db_filename"])
        try:
            await _seed(db)
        finally:
            await close_user_db(db)
    finally:
        # aiosqlite keeps a non-daemon thread alive; close it so the process exits.
        await close_central_db()

    print("\nDemo account ready. Log in and take screenshots:")
    print(f"  email:    {DEMO_EMAIL}")
    print(f"  password: {DEMO_PASSWORD}")
    print("\nAll data is fictional. It lives in the gitignored data/ dir — do not commit it.")


if __name__ == "__main__":
    asyncio.run(main())
