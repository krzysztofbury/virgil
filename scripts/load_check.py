"""Load check for uploads and background jobs on the deployment hardware.

Runs entirely inside a throwaway container from the production image, with no
network: it seeds accounts with about a year of synthetic data in a temporary
directory, serves them with the production server on a loopback port, and drives
it with simulated users. Nothing touches the real data, and nothing leaves the
container. Run on the NAS, from any directory:

    curl -fsSL https://raw.githubusercontent.com/krzysztofbury/virgil/main/scripts/load_check.py \\
      | docker run --rm -i --network none --entrypoint /app/.venv/bin/python \\
          ghcr.io/krzysztofbury/virgil:latest - --users 3 --clients 12 --seconds 90

Phases: reads only, then reads with saves, manual backups and medical PDF
uploads (the one upload endpoint) all at once, then waiting for the job queue to
drain. It prints latency per request kind, status counts, job outcomes and the
server's peak memory, as a Markdown block to paste into docs/load-check.md.

Medical imports fail at their provider call (no network, and a placeholder key):
the check covers accepting the upload, staging it and running the job, not the
provider. Locally: `uv run --locked python scripts/load_check.py --seconds 20`.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() and Path(__file__).exists() else Path.cwd()
sys.path.insert(0, str(ROOT))

PASSWORD = "load-check-password-1"  # pragma: allowlist secret - synthetic accounts in a temporary directory
# Spacing between simulated clicks. SystemRandom only because it is the one
# bandit accepts; nothing here needs to be unpredictable.
_random = secrets.SystemRandom()
READ_PATHS = ["/", "/daily", "/training", "/oura", "/bloodwork", "/goals", "/experiments", "/freedom"]
JOB_KINDS = ("backup", "medical_import")


def _environment(work: Path, port: int) -> dict:
    return {
        **os.environ,
        "VIRGIL_ENV": "prod",
        "VIRGIL_BASE_URL": f"http://127.0.0.1:{port}",
        "VIRGIL_CENTRAL_DB_PATH": str(work / "data" / "virgil-central.db"),
        "VIRGIL_BACKUP_DIR": str(work / "backups"),
        "VIRGIL_REGISTRATION_OPEN": "false",
        # Each simulated user gets an address of its own, as behind Cloudflare,
        # so the per-address rate limit applies per user and not to the check.
        "VIRGIL_TRUST_CLOUDFLARE_HEADERS": "true",
        "VIRGIL_TRUSTED_PROXY_IPS": "127.0.0.1",
        # A placeholder: the upload is accepted and queued, the provider call fails.
        "VIRGIL_INTERNAL_LLM_KEY": "load-check-placeholder",
        "VIRGIL_API_KEY": "",
        "VIRGIL_ALERT_WEBHOOK_URL": "",
        "LITELLM_LOCAL_MODEL_COST_MAP": "true",
    }


async def _seed(users: int) -> list[str]:
    """Accounts with roughly a year of daily use each."""
    from app.central_db import close_central_db, create_user, init_central_db
    from app.user_db import close_user_db, create_user_db, open_user_db

    emails = []
    await init_central_db()
    try:
        for n in range(users):
            email = f"load{n}@example.test"
            user = await create_user(email, PASSWORD, f"Load {n}")
            await create_user_db(user["db_filename"])
            db = await open_user_db(user["db_filename"])
            try:
                await db.executemany(
                    "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                    [("onboarding_completed", "1"), ("feature_freedom", "1"), ("backup_enabled", "0")],
                )
                today = date.today()
                days = [(today - timedelta(days=i)).isoformat() for i in range(365)]
                await db.executemany(
                    "INSERT OR IGNORE INTO oura_daily (date, sleep_score, readiness_score, activity_score, steps, "
                    "sleep_duration_hours, resting_hr, avg_hrv) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        (d, 70 + i % 20, 72 + i % 15, 75 + i % 10, 6000 + i * 7 % 6000, 7.1, 55, 48)
                        for i, d in enumerate(days)
                    ],
                )
                await db.executemany(
                    "INSERT OR IGNORE INTO daily_logs (date, energy, morning_routine, water) VALUES (?, ?, ?, ?)",
                    [(d, 4 + i % 6, "done" if i % 3 else "skipped", "done") for i, d in enumerate(days)],
                )
                exercises = [row[0] for row in await db.execute_fetchall("SELECT id FROM training_exercises LIMIT 6")]
                for i, d in enumerate(days[::2]):
                    cursor = await db.execute(
                        "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 60, ?)",
                        (d, f"Synthetic session {i}"),
                    )
                    await db.executemany(
                        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [
                            (cursor.lastrowid, exercises[s % len(exercises)], s + 1, 5 + s, 40 + s * 2.5)
                            for s in range(10)
                        ],
                    )
                await db.commit()
            finally:
                await close_user_db(db)
            emails.append(email)
    finally:
        await close_central_db()
    return emails


class Recorder:
    def __init__(self):
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.statuses: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.errors: dict[str, int] = defaultdict(int)

    def add(self, kind: str, seconds: float, status: int):
        self.latency[kind].append(seconds)
        self.statuses[kind][status] += 1

    def table(self) -> list[str]:
        rows = [
            "| Request | Count | p50 ms | p95 ms | max ms | Statuses |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for kind in sorted(self.latency):
            values = sorted(self.latency[kind])
            p95 = values[max(0, int(len(values) * 0.95) - 1)]
            statuses = ", ".join(f"{code}: {n}" for code, n in sorted(self.statuses[kind].items()))
            errors = f", errors: {self.errors[kind]}" if self.errors[kind] else ""
            rows.append(
                f"| {kind} | {len(values)} | {statistics.median(values) * 1000:.0f} | {p95 * 1000:.0f} | "
                f"{values[-1] * 1000:.0f} | {statuses}{errors} |"
            )
        return rows


class Session:
    """One simulated user: their own client, address and session cookie."""

    def __init__(self, base: str, email: str, address: str, recorder: Recorder):
        import httpx

        self.email, self.recorder = email, recorder
        self.client = httpx.AsyncClient(
            base_url=base, headers={"CF-Connecting-IP": address}, timeout=120, follow_redirects=False
        )
        self.csrf = ""

    async def request(self, kind: str, method: str, path: str, **kwargs):
        started = time.perf_counter()
        try:
            response = await self.client.request(method, path, **kwargs)
        except Exception as exc:  # a refused or reset connection is a finding, not a crash
            self.recorder.errors[kind] += 1
            self.recorder.add(kind, time.perf_counter() - started, 0)
            print(f"  {kind}: {type(exc).__name__}", file=sys.stderr)
            return None
        self.recorder.add(kind, time.perf_counter() - started, response.status_code)
        token = re.search(r'name="csrf-token" content="([^"]+)"', response.text or "")
        if token:
            self.csrf = token.group(1)
        return response

    async def sign_in(self):
        page = await self.client.get("/login")
        token = re.search(r'name="_csrf_token" value="([^"]+)"', page.text).group(1)
        response = await self.client.post(
            "/login", data={"_csrf_token": token, "username": self.email, "password": PASSWORD}
        )
        if response.status_code != 303 or "/login" in response.headers.get("location", ""):
            raise SystemExit(f"Sign-in failed for {self.email}: {response.status_code}")
        await self.request("read", "GET", "/daily")

    async def read(self):
        await self.request("read", "GET", _random.choice(READ_PATHS))

    async def save_daily(self):
        await self.request(
            "daily save",
            "POST",
            "/daily/save",
            data={"_csrf_token": self.csrf, "date": date.today().isoformat(), "energy": str(_random.randint(1, 10))},
            headers={"HX-Request": "true", "X-CSRF-Token": self.csrf},
        )

    async def backup_now(self):
        await self.request(
            "backup now",
            "POST",
            "/settings/backup/now",
            data={"_csrf_token": self.csrf, "job_nonce": secrets.token_hex(16)},
        )

    async def upload(self, megabytes: float):
        body = b"%PDF-1.4\n" + os.urandom(int(megabytes * 1024 * 1024))
        await self.request(
            f"upload {megabytes:g} MB",
            "POST",
            "/onboarding/step/5",
            data={"_csrf_token": self.csrf, "medical_text": ""},
            files={"medical_file": ("results.pdf", body, "application/pdf")},
        )

    async def close(self):
        await self.client.aclose()


async def _reader(session: Session, until: float):
    while time.monotonic() < until:
        await session.read()
        await asyncio.sleep(_random.uniform(0.5, 1.5))  # a person reading, not a benchmark


async def _probe(base: str, recorder_of, until: float):
    """/livez four times a second: it does no work, so its latency is the
    server's own stalls, not any page's cost."""
    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        n = 0
        while time.monotonic() < until:
            n += 1
            started = time.perf_counter()
            try:
                # An address per request: the probe must not meet the rate limit.
                response = await client.get("/livez", headers={"CF-Connecting-IP": f"203.0.113.{n % 250 + 1}"})
                recorder_of().add("liveness probe", time.perf_counter() - started, response.status_code)
            except Exception:
                recorder_of().errors["liveness probe"] += 1
            await asyncio.sleep(0.25)


async def _writer(session: Session, until: float, upload_mb: float):
    """A person logging: saves most often, now and then a backup or an upload."""
    cycle = [session.save_daily, lambda: session.upload(upload_mb), session.save_daily, session.backup_now]
    if not upload_mb:
        cycle.pop(1)
    step = 0
    while time.monotonic() < until:
        await cycle[step % len(cycle)]()
        step += 1
        await asyncio.sleep(_random.uniform(2, 4))


def _memory(pid: int) -> dict[str, int]:
    fields = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "VmHWM"}:
                fields[key] = int(value.split()[0]) // 1024
    except OSError:
        pass
    return fields


def _jobs(work: Path) -> list[str]:
    rows = defaultdict(lambda: defaultdict(int))
    durations = defaultdict(list)
    for path in sorted((work / "data" / "users").glob("*.db")):
        with sqlite3.connect(path) as db:
            for kind, status, seconds in db.execute(
                "SELECT kind, status, (julianday(finished_at) - julianday(created_at)) * 86400 FROM jobs"
            ):
                rows[kind][status] += 1
                if seconds is not None:
                    durations[kind].append(seconds)
    lines = ["| Job | Outcomes | Queued to finished, median s | max s |", "| --- | --- | ---: | ---: |"]
    for kind in sorted(rows):
        outcomes = ", ".join(f"{status}: {n}" for status, n in sorted(rows[kind].items()))
        times = durations[kind]
        lines.append(
            f"| {kind} | {outcomes} | {statistics.median(times):.1f} | {max(times):.1f} |"
            if times
            else f"| {kind} | {outcomes} | - | - |"
        )
    return lines


async def _drive(base: str, emails: list[str], clients: int, seconds: float, upload_mb: float, pid: int) -> dict:
    recorder = Recorder()
    sessions = [Session(base, emails[n % len(emails)], f"198.51.100.{n + 1}", recorder) for n in range(clients)]
    peak = {"VmRSS": 0}

    async def sample():
        while True:
            peak["VmRSS"] = max(peak["VmRSS"], _memory(pid).get("VmRSS", 0))
            await asyncio.sleep(0.5)

    sampler = asyncio.create_task(sample())
    try:
        await asyncio.gather(*(s.sign_in() for s in sessions))
        phases = {}
        current = {"recorder": recorder}
        until = time.monotonic() + seconds / 2
        await asyncio.gather(*(_reader(s, until) for s in sessions), _probe(base, lambda: current["recorder"], until))
        phases["reads"] = recorder.table()
        recorder_mixed = Recorder()
        current["recorder"] = recorder_mixed
        for s in sessions:
            s.recorder = recorder_mixed
        until = time.monotonic() + seconds / 2
        writers = sessions[: max(1, len(emails))]
        await asyncio.gather(
            *(_reader(s, until) for s in sessions),
            *(_writer(s, until, upload_mb) for s in writers),
            _probe(base, lambda: current["recorder"], until),
        )
        phases["mixed"] = recorder_mixed.table()
        return {"phases": phases, "peak_rss_mb": peak["VmRSS"]}
    finally:
        sampler.cancel()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


def _wait_until_up(port: int, server: subprocess.Popen, timeout: float = 120):
    import http.client

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit("The server exited during startup")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/livez")
            if connection.getresponse().status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("The server did not come up in time")


def _drain(work: Path, timeout: float) -> float:
    """Seconds until no backup or import job is queued or running."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        active = 0
        for path in (work / "data" / "users").glob("*.db"):
            with sqlite3.connect(path) as db:
                active += db.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running') AND kind IN {JOB_KINDS}"
                ).fetchone()[0]
        if not active:
            return time.monotonic() - started
        time.sleep(1)
    return float("nan")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--users", type=int, default=3, help="accounts, each with about a year of data")
    parser.add_argument("--clients", type=int, default=12, help="simulated people reading at once")
    parser.add_argument("--seconds", type=float, default=90, help="length of the two load phases together")
    parser.add_argument("--upload-mb", type=float, default=10, help="size of each synthetic medical PDF; 0 for none")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="virgil-load-"))
    env = _environment(work, args.port)
    os.environ.update(env)
    server = None
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(130)))
    try:
        (work / "data" / "users").mkdir(parents=True)
        (work / "backups").mkdir()
        started = time.monotonic()
        emails = asyncio.run(_seed(args.users))
        seeded = time.monotonic() - started
        server = subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(work / "server.log", "w"),  # noqa: SIM115 - closed with the process
        )
        _wait_until_up(args.port, server)
        result = asyncio.run(
            _drive(f"http://127.0.0.1:{args.port}", emails, args.clients, args.seconds, args.upload_mb, server.pid)
        )
        drained = _drain(work, timeout=300)
        memory = _memory(server.pid)
        sizes = sum(p.stat().st_size for p in (work / "data").rglob("*.db")) / 1e6
        log = (work / "server.log").read_text(errors="replace")
        # The exception lines, not the traceback frames around them.
        problems = sorted(
            {
                line.strip(" │╰╯─")[:160]
                for line in log.splitlines()
                if re.search(r"\b\w+(Error|Exception)\b: ", line) or " ERROR " in line
            }
        )

        cpu = os.cpu_count()
        machine = os.uname()
        lines = [
            f"### Load check {date.today().isoformat()}",
            "",
            f"- Host: {machine.machine}, {cpu} CPU(s), kernel {machine.release}; "
            f"image version {os.environ.get('VIRGIL_GIT_SHA', 'local')}",
            f"- {args.users} account(s), {sizes:.1f} MB of databases (seeded in {seeded:.0f} s); "
            f"{args.clients} simulated readers, {args.seconds:g} s; uploads of {args.upload_mb:g} MB",
            f"- Server memory: peak during load {result['peak_rss_mb']} MB, "
            f"high-water {memory.get('VmHWM', '?')} MB, after {memory.get('VmRSS', '?')} MB",
            f"- Job queue drained {drained:.0f} s after the load stopped",
            "",
            "Reads only:",
            "",
            *result["phases"]["reads"],
            "",
            "Reads with saves, backups and uploads:",
            "",
            *result["phases"]["mixed"],
            "",
            "Jobs:",
            "",
            *_jobs(work),
            "",
            f"Server log errors ({len(problems)} distinct):" if problems else "Server log: no errors.",
            *[f"- `{line}`" for line in problems[:15]],
        ]
        print("\n".join(lines))
        print("\n" + json.dumps({"peak_rss_mb": result["peak_rss_mb"], "drained_s": drained}), file=sys.stderr)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
