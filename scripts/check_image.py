"""Smoke-test a production image with isolated storage and no external network."""

import signal
import subprocess
import sys
import time
import uuid

PROBE = r"""
import http.cookiejar, re, urllib.parse, urllib.request
base = 'http://127.0.0.1:8123'
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
assert client.open(base + '/healthz', timeout=5).status == 200
page = client.open(base + '/signup', timeout=5).read().decode()
token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
data = urllib.parse.urlencode(dict(email='smoke@example.com', display_name='Smoke',
    # pragma: allowlist nextline secret - synthetic fixture
    password='synthetic-smoke-password', password_confirm='synthetic-smoke-password', _csrf_token=token)).encode()
assert client.open(base + '/signup', data=data, timeout=10).status == 200
assert client.open(base + '/static/css/app.css', timeout=5).status == 200
assert client.open(base + '/healthz', timeout=5).status == 200
print('Signup, migrations, templates, and static assets verified.')
"""

LOGIN = r"""
import http.cookiejar, re, urllib.parse, urllib.request
base = 'http://127.0.0.1:8123'
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
page = client.open(base + '/login', timeout=5).read().decode()
token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
# pragma: allowlist nextline secret - synthetic fixture
data = urllib.parse.urlencode(dict(username='smoke@example.com', password='synthetic-smoke-password',
    _csrf_token=token)).encode()
response = client.open(base + '/login', data=data, timeout=10)
assert urllib.parse.urlparse(response.url).path in {'/', '/onboarding'}, response.url
response = client.open(base + '/settings', timeout=5)
assert '/login' not in response.url
print('Restored account login and user database verified.')
"""


def docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=True, timeout=60, **kwargs)


def wait_healthy(name: str) -> None:
    health = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8123/healthz', timeout=2)"
    for _ in range(30):
        result = subprocess.run(
            ["docker", "exec", name, "/app/.venv/bin/python", "-c", health],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Image failed its startup health check")


def check(image: str) -> None:
    name = f"virgil-smoke-{uuid.uuid4().hex}"
    volume = name + "-data"
    try:
        docker("volume", "create", volume, stdout=subprocess.DEVNULL)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume},target=/data",
            "-e",
            "VIRGIL_CENTRAL_DB_PATH=/data/virgil-central.db",
            "-e",
            "VIRGIL_INTERNAL_LLM_KEY=",
            "-e",
            "VIRGIL_REGISTRATION_OPEN=false",
            image,
            stdout=subprocess.DEVNULL,
        )
        wait_healthy(name)
        docker("exec", name, "/app/.venv/bin/python", "-c", PROBE)
        docker("restart", name, stdout=subprocess.DEVNULL)
        wait_healthy(name)
        docker("stop", name, stdout=subprocess.DEVNULL)
        for arguments in (
            ("create", "--central", "/data/virgil-central.db", "--destination", "/data/bundle", "--stopped"),
            ("verify", "/data/bundle"),
            ("restore", "/data/bundle", "--destination", "/data/restored"),
        ):
            docker(
                "run",
                "--rm",
                "--name",
                name + "-operator",
                "--network",
                "none",
                "--mount",
                f"type=volume,source={volume},target=/data",
                image,
                "/app/.venv/bin/python",
                "scripts/recovery.py",
                *arguments,
            )
        docker("rm", name, stdout=subprocess.DEVNULL)
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--mount",
            f"type=volume,source={volume},target=/data",
            "-e",
            "VIRGIL_CENTRAL_DB_PATH=/data/restored/virgil-central.db",
            "-e",
            "VIRGIL_INTERNAL_LLM_KEY=",
            "-e",
            "VIRGIL_REGISTRATION_OPEN=false",
            image,
            stdout=subprocess.DEVNULL,
        )
        wait_healthy(name)
        docker("exec", name, "/app/.venv/bin/python", "-c", LOGIN)
        print("Production image verified, including restart and offline recovery.")
    except BaseException:
        subprocess.run(["docker", "logs", name], check=False, timeout=10)
        raise
    finally:
        # Cleanup only the uniquely named resources owned by this invocation.
        # The operator normally removes itself, but may outlive an interrupted CLI.
        subprocess.run(
            ["docker", "rm", "--force", name + "-operator"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(["docker", "rm", "--force", name], check=False, timeout=30)
        result = subprocess.run(["docker", "volume", "rm", volume], check=False, timeout=30)
        if result.returncode:
            raise RuntimeError(f"Smoke cleanup failed; inspect owned volume {volume}")


if __name__ == "__main__":

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    check(sys.argv[1])
