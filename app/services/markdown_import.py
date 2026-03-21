"""Import data from existing markdown files into SQLite."""

import contextlib
import os
import re
from datetime import date

from app.config import SECOND_BRAIN_PATH


async def import_liczby(db):
    """Parse liczby.md -> daily_logs + body_measurements."""
    path = os.path.join(SECOND_BRAIN_PATH, "liczby.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    current_date = None
    current_log = {}
    measurements = {}

    # Parse year from file header (e.g. "# 10 Feb - 16 Feb 2026") or week headers
    current_year = None
    year_match = re.search(r"\b(20\d{2})\b", content.split("\n")[0] if content else "")
    if year_match:
        current_year = int(year_match.group(1))

    for line in content.split("\n"):
        # Week header may contain year: ## 10.02 - 16.02.2026
        ym = re.search(r"\b(20\d{2})\b", line) if line.startswith("## ") else None
        if ym:
            current_year = int(ym.group(1))

        # Date header: ### 15.02 (Niedziela - ...)
        m = re.match(r"^### (\d{2})\.(\d{2})\s*\(", line)
        if m:
            if current_date and current_log:
                await _save_daily_log(db, current_date, current_log)
                if measurements:
                    await _save_measurements(db, current_date, measurements)
            day, month = int(m.group(1)), int(m.group(2))
            year = current_year or date.today().year
            current_date = f"{year}-{month:02d}-{day:02d}"
            current_log = {}
            measurements = {}
            continue

        if not current_date:
            continue

        # Energy
        em = re.match(r"- Energy:\s*(\d+)/10", line)
        if em:
            current_log["energy"] = int(em.group(1))

        # Routines and toggles
        toggle_patterns = [
            (r"- Rutyna Poranna.*:\s*\[(.)\]", "morning_routine"),
            (r"- Rutyna Wieczorna.*:\s*\[(.)\]", "evening_routine"),
            (r"- Water Intake.*:\s*\[(.)\]", "water"),
        ]
        for pattern, key in toggle_patterns:
            mt = re.match(pattern, line)
            if mt:
                cb = mt.group(1)
                current_log[key] = "done" if cb == "x" else "skipped" if cb == "-" else "pending"

        # A.N.D.Y. items
        andy_patterns = [
            (r"- A\.N\.D\.Y\. Ciało:\s*\[(.)\]\s*(.*)", "andy_body_status", "andy_body_desc"),
            (r"- A\.N\.D\.Y\. Duch:\s*\[(.)\]\s*(.*)", "andy_spirit_status", "andy_spirit_desc"),
            (r"- A\.N\.D\.Y\. Konto:\s*\[(.)\]\s*(.*)", "andy_account_status", "andy_account_desc"),
            (r"- A\.N\.D\.Y\. Relacje:\s*\[(.)\]\s*(.*)", "andy_relations_status", "andy_relations_desc"),
        ]
        for pattern, status_key, desc_key in andy_patterns:
            am = re.match(pattern, line)
            if am:
                cb = am.group(1)
                current_log[status_key] = "done" if cb == "x" else "skipped" if cb == "-" else "pending"
                current_log[desc_key] = am.group(2).strip()

        # Measurements
        meas_patterns = [
            (r"\s*- Waga:\s*(.+)", "weight"),
            (r"\s*- Ramię:\s*(.+)", "arm"),
            (r"\s*- Talia:\s*(.+)", "waist"),
            (r"\s*- Biodra:\s*(.+)", "hips"),
            (r"\s*- Uda:\s*(.+)", "thighs"),
        ]
        for pattern, key in meas_patterns:
            mm = re.match(pattern, line)
            if mm:
                val = mm.group(1).strip()
                if val:
                    with contextlib.suppress(ValueError):
                        measurements[key] = float(val.replace(",", "."))

    if current_date and current_log:
        await _save_daily_log(db, current_date, current_log)
        if measurements:
            await _save_measurements(db, current_date, measurements)

    await db.commit()


async def _save_daily_log(db, date_str, log):
    await db.execute(
        """
        INSERT OR IGNORE INTO daily_logs (date, energy, morning_routine, evening_routine, water,
            andy_body_status, andy_body_desc, andy_spirit_status, andy_spirit_desc,
            andy_account_status, andy_account_desc, andy_relations_status, andy_relations_desc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            date_str,
            log.get("energy"),
            log.get("morning_routine", "pending"),
            log.get("evening_routine", "pending"),
            log.get("water", "pending"),
            log.get("andy_body_status", "pending"),
            log.get("andy_body_desc", ""),
            log.get("andy_spirit_status", "pending"),
            log.get("andy_spirit_desc", ""),
            log.get("andy_account_status", "pending"),
            log.get("andy_account_desc", ""),
            log.get("andy_relations_status", "pending"),
            log.get("andy_relations_desc", ""),
        ),
    )


async def _save_measurements(db, date_str, meas):
    await db.execute(
        """
        INSERT OR IGNORE INTO body_measurements (date, weight, arm, waist, hips, thighs)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (date_str, meas.get("weight"), meas.get("arm"), meas.get("waist"), meas.get("hips"), meas.get("thighs")),
    )


async def import_noporn(db):
    """Parse noporn.md -> feniks_journal + feniks_pleasures + milestone completions."""
    path = os.path.join(SECOND_BRAIN_PATH, "noporn.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Parse journal table
    in_journal = False
    header_count = 0
    for line in content.split("\n"):
        if "Tabela z Ogniskiem" in line:
            in_journal = True
            header_count = 0
            continue
        if in_journal:
            if line.startswith("|") and "---" in line:
                header_count += 1
                continue
            if line.startswith("|") and "Data" in line:
                continue
            m = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)", line)
            if m:
                date_str = m.group(1)
                cols = [c.strip() for c in m.group(2).split("|")]
                if len(cols) >= 5:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO feniks_journal (date, emotions, triggers, thoughts, desired_feelings, coping_strategies)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (date_str, cols[0], cols[1], cols[2], cols[3], cols[4] if len(cols) > 4 else ""),
                    )
            elif not line.startswith("|"):
                in_journal = False

    # Parse pleasures table
    in_pleasures = False
    for line in content.split("\n"):
        if "Dwie Przyjemności" in line:
            in_pleasures = True
            continue
        if in_pleasures:
            if line.startswith("|") and ("---" in line or "Data" in line):
                continue
            m = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)", line)
            if m:
                date_str = m.group(1)
                cols = [c.strip() for c in m.group(2).split("|")]
                p1 = cols[0] if cols else ""
                p2 = cols[1] if len(cols) > 1 else ""
                if p1 or p2:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO feniks_pleasures (date, pleasure_1, pleasure_2)
                        VALUES (?, ?, ?)
                    """,
                        (date_str, p1, p2),
                    )
            elif not line.startswith("|"):
                in_pleasures = False

    # Parse milestone completions
    for line in content.split("\n"):
        m = re.match(r"- \[(.)\] \*\*Dzień (\d+):\*\*", line)
        if m:
            cb = m.group(1)
            day_num = int(m.group(2))
            completed = 1 if cb == "x" else 0
            await db.execute("UPDATE feniks_milestones SET completed = ? WHERE day_number = ?", (completed, day_num))

    await db.commit()


async def import_oura(db):
    """Parse oura.md -> oura_monthly."""
    path = os.path.join(SECOND_BRAIN_PATH, "oura.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Build a dict of month -> {metric: value}
    data = {}
    current_section = None

    section_map = {
        "Sleep Score": "sleep_score",
        "Readiness Score": "readiness",
        "Readiness": "readiness",
        "Activity Score": "activity",
        "Activity": "activity",
        "Steps": "steps",
        "Sleep Duration": "sleep_duration",
        "Deep Sleep": "deep_sleep",
        "REM Sleep": "rem_sleep",
        "Resting Heart Rate": "rhr",
        "Lowest Heart Rate": "lowest_hr",
        "HRV": "hrv",
        "Cardiovascular Age": "cardiovascular_age",
        "Stress": "stress",
    }

    for line in content.split("\n"):
        # Section header
        hm = re.match(r"^###\s+(.+)", line)
        if hm:
            title = hm.group(1).strip()
            for key, val in section_map.items():
                if title.startswith(key):
                    current_section = val
                    break
            else:
                current_section = None
            continue

        if not current_section:
            continue

        # Data line: * 2022-08: 84 or - 2022-08: 84
        dm = re.match(r"^[*-]\s+(\d{4}-\d{2}):\s*(.+)", line)
        if dm:
            month = dm.group(1)
            val_str = dm.group(2).strip()

            if month not in data:
                data[month] = {"notes": ""}

            if current_section == "stress":
                # Parse: 17 normal, 3 stressful, 2 restored
                sm = re.match(r"(\d+)\s+normal.*?(\d+)\s+stressful.*?(\d+)\s+restored", val_str)
                if sm:
                    data[month]["stress_normal"] = int(sm.group(1))
                    data[month]["stress_stressful"] = int(sm.group(2))
                    data[month]["stress_restored"] = int(sm.group(3))
            else:
                # Extract numeric value and optional notes
                notes_match = re.search(r"\(([^)]+)\)", val_str)
                if notes_match:
                    data[month]["notes"] = notes_match.group(1)

                val_clean = re.sub(r"\s*(h|bpm|ms)?\s*(\(.*\))?$", "", val_str).strip()
                try:
                    val = float(val_clean.replace(",", "."))
                    data[month][current_section] = val
                except ValueError:
                    pass

    # Allowlist of valid oura_monthly columns to prevent SQL injection via f-strings
    valid_columns = {
        "sleep_score",
        "readiness",
        "activity",
        "steps",
        "sleep_duration",
        "deep_sleep",
        "rem_sleep",
        "rhr",
        "lowest_hr",
        "hrv",
        "cardiovascular_age",
        "stress_normal",
        "stress_stressful",
        "stress_restored",
    }

    for month, values in data.items():
        notes = values.pop("notes", "")
        cols = [c for c in values if c in valid_columns]
        if not cols:
            continue

        placeholders = ", ".join([f"{c}=excluded.{c}" for c in cols])
        col_names = ", ".join(["month", "notes"] + cols)
        val_places = ", ".join(["?"] * (2 + len(cols)))
        vals = [month, notes] + [values[c] for c in cols]

        await db.execute(
            f"""
            INSERT INTO oura_monthly ({col_names}) VALUES ({val_places})
            ON CONFLICT(month) DO UPDATE SET {placeholders}, notes=excluded.notes
        """,
            vals,
        )

    await db.commit()


async def import_badania(db):
    """Parse badania.md -> blood_markers + blood_results."""
    path = os.path.join(SECOND_BRAIN_PATH, "badania.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Map known markers to categories
    marker_categories = {
        "Leukocyty (WBC)": "Morfologia",
        "Erytrocyty (RBC)": "Morfologia",
        "Hemoglobina (HGB)": "Morfologia",
        "Hematokryt (HCT)": "Morfologia",
        "Płytki krwi (PLT)": "Morfologia",
        "Morfologia - parametry czerwonokrwinkowe (MCV, MCH, MCHC)": "Morfologia",
        "Morfologia - rozmaz (ilościowo)": "Morfologia",
        "TSH": "Tarczyca i Hormony",
        "FT3": "Tarczyca i Hormony",
        "FT4": "Tarczyca i Hormony",
        "Testosteron": "Tarczyca i Hormony",
        "Prolaktyna": "Tarczyca i Hormony",
        "SHBG": "Tarczyca i Hormony",
        "Kortyzol": "Tarczyca i Hormony",
        "Glukoza (na czczo)": "Metabolizm",
        "Hemoglobina glikowana (HbA1c)": "Metabolizm",
        "Insulina": "Metabolizm",
        "Cholesterol całkowity": "Lipidy",
        "Cholesterol LDL": "Lipidy",
        "Cholesterol HDL": "Lipidy",
        "Cholesterol nie-HDL": "Lipidy",
        "Triglicerydy (TG)": "Lipidy",
        "Lipoproteina Lp(a)": "Lipidy",
        "Apo B": "Lipidy",
        "Witamina D3 (Metabolit 25(OH))": "Witaminy i Minerały",
        "Ferrytyna": "Witaminy i Minerały",
        "Żelazo": "Witaminy i Minerały",
        "Witamina B12": "Witaminy i Minerały",
        "Kwas foliowy": "Witaminy i Minerały",
        "Homocysteina": "Witaminy i Minerały",
        "CRP (wysokiej czułości)": "Stany zapalne",
        "ALT": "Wątroba i Nerki",
        "AST": "Wątroba i Nerki",
        "GGTP": "Wątroba i Nerki",
        "Bilirubina całkowita": "Wątroba i Nerki",
        "Fosfataza zasadowa": "Wątroba i Nerki",
        "Kinaza kreatynowa (CK)": "Wątroba i Nerki",
        "Kreatynina": "Wątroba i Nerki",
        "eGFR": "Wątroba i Nerki",
        "Kwas moczowy": "Wątroba i Nerki",
        "Amylaza": "Wątroba i Nerki",
        "Lipaza": "Wątroba i Nerki",
        "Magnez": "Witaminy i Minerały",
        "Elektrolity (Sód, Potas)": "Witaminy i Minerały",
    }

    current_marker = None
    display_order = 0

    for line in content.split("\n"):
        # Marker header
        hm = re.match(r"^###\s+(.+)", line)
        if hm:
            name = hm.group(1).strip()
            category = marker_categories.get(name, "Inne")
            display_order += 1

            # Extract unit from first result line (next pass)
            current_marker = {"name": name, "category": category, "order": display_order}
            continue

        if not current_marker:
            continue

        # Result line: * 2020-12-30: 5,87 tys/µl or * 2025-12-04: MCV 91,2 fl
        rm = re.match(r"^[*-]\s+(\d{4}-\d{2}-\d{2}):\s*(.+)", line)
        if rm:
            date_str = rm.group(1)
            val_str = rm.group(2).strip()

            # Handle composite entries like "MCV 91,2 fl"
            # Handle flag (H) or (L)
            flag = ""
            if "(H)" in val_str:
                flag = "H"
                val_str = val_str.replace("(H)", "").strip()
            elif "(L)" in val_str:
                flag = "L"
                val_str = val_str.replace("(L)", "").strip()

            # Remove notes in parentheses like (godziny poranne)
            val_str = re.sub(r"\([^HL][^)]*\)", "", val_str).strip()

            # Try to extract numeric value and unit
            vm = re.match(r"[A-Za-z-]*\s*<?(\d+[.,]?\d*)\s*(.*)", val_str)
            if vm:
                try:
                    value = float(vm.group(1).replace(",", "."))
                    unit = vm.group(2).strip()

                    # Ensure marker exists
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO blood_markers (name, category, unit, display_order)
                        VALUES (?, ?, ?, ?)
                    """,
                        (current_marker["name"], current_marker["category"], unit or "?", current_marker["order"]),
                    )

                    # Get marker id
                    row = await db.execute_fetchall(
                        "SELECT id FROM blood_markers WHERE name = ?", (current_marker["name"],)
                    )
                    if row:
                        marker_id = row[0]["id"]
                        value_text = ""
                        if val_str.startswith("<") or ">90" in val_str or ">=" in val_str:
                            value_text = val_str

                        await db.execute(
                            """
                            INSERT OR IGNORE INTO blood_results (marker_id, date, value, value_text, flag)
                            VALUES (?, ?, ?, ?, ?)
                        """,
                            (marker_id, date_str, value, value_text, flag),
                        )
                except ValueError:
                    pass
        elif not line.startswith("*") and not line.startswith("-") and line.strip() and not line.startswith("#"):
            current_marker = None

    await db.commit()


async def import_snapply(db):
    """Parse snapply.md -> life_scores."""
    path = os.path.join(SECOND_BRAIN_PATH, "snapply.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Extract the most recent report's data
    date_match = re.search(r"\*\*Data diagnozy:\*\*\s*(\d{2})\.(\d{2})\.(\d{4})", content)
    power_match = re.search(r"\*\*Power Level:\*\*\s*([\d.]+)/10", content)

    if not date_match:
        return

    d, m, y = date_match.group(1), date_match.group(2), date_match.group(3)
    date_str = f"{y}-{m}-{d}"

    power_level = float(power_match.group(1)) if power_match else None

    area_map = {
        "PLANOWANIE ŻYCIA": "planning",
        "DUCHOWOŚĆ": "spirituality",
        "ZDROWIE": "health",
        "PRACA": "work",
        "ŻYCIE TOWARZYSKIE": "social",
        "ROZWÓJ": "growth",
        "RELAKS": "relaxation",
        "RODZINA": "family",
    }

    scores = {}
    for line in content.split("\n"):
        for label, key in area_map.items():
            if label in line:
                sm = re.search(r"(\d+)/10", line)
                if sm:
                    scores[key] = int(sm.group(1))

    # Extract metrics
    weight_match = re.search(r"\*\*Waga:\*\*\s*([\d.]+)", content)
    waist_match = re.search(r"\*\*Talia:\*\*\s*([\d.]+)", content)

    await db.execute(
        """
        INSERT OR IGNORE INTO life_scores (date, planning, spirituality, health, work, social,
            growth, relaxation, family, power_level, weight, waist)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            date_str,
            scores.get("planning"),
            scores.get("spirituality"),
            scores.get("health"),
            scores.get("work"),
            scores.get("social"),
            scores.get("growth"),
            scores.get("relaxation"),
            scores.get("family"),
            power_level,
            float(weight_match.group(1)) if weight_match else None,
            float(waist_match.group(1)) if waist_match else None,
        ),
    )

    await db.commit()


async def import_cele(db):
    """Parse cele.md -> goals."""
    path = os.path.join(SECOND_BRAIN_PATH, "cele.md")
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        content = f.read()

    area_map = {
        "PLANOWANIE ŻYCIA": "Planowanie Życia",
        "DUCHOWOŚĆ": "Duchowość",
        "ZDROWIE": "Zdrowie",
        "PRACA": "Praca",
        "ŻYCIE TOWARZYSKIE": "Życie Towarzyskie",
        "ROZWÓJ": "Rozwój",
        "RELAKS": "Relaks",
        "RODZINA": "Rodzina",
    }

    current_area = None
    current_horizon = None
    order = 0

    for line in content.split("\n"):
        # Area header
        am = re.match(r"^## .+ OBSZAR \d+: (.+)", line)
        if am:
            area_label = am.group(1).strip()
            for key, name in area_map.items():
                if key in area_label:
                    current_area = name
                    break
            continue

        # Horizon
        hm = re.match(r"^\*\*Poziom \d+ \((\d+)\s*(rok|lata?|lat)", line)
        if hm:
            num = int(hm.group(1))
            if num == 1:
                current_horizon = "1yr"
            elif num == 3:
                current_horizon = "3yr"
            elif num == 10:
                current_horizon = "10yr"
            order = 0
            continue

        # Goal item
        if current_area and current_horizon and line.startswith("- "):
            content_text = line[2:].strip()
            if content_text:
                # Get area id
                row = await db.execute_fetchall("SELECT id FROM goal_areas WHERE name = ?", (current_area,))
                if row:
                    area_id = row[0]["id"]
                    order += 1
                    await db.execute(
                        "INSERT INTO goals (area_id, horizon, content, display_order) VALUES (?, ?, ?, ?)",
                        (area_id, current_horizon, content_text, order),
                    )

    await db.commit()


async def import_all(db):
    await import_badania(db)
    await import_oura(db)
    await import_liczby(db)
    await import_noporn(db)
    await import_snapply(db)
    await import_cele(db)
