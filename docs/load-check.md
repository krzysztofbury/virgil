# Load check: uploads and background jobs

`scripts/load_check.py` measures how the production image handles uploads and
background jobs alongside ordinary use, on the hardware it runs on. It runs in a
throwaway container with no network. It seeds accounts with about a year of
synthetic data each in a temporary directory, serves them with the production
server on a loopback port, and drives that server with simulated people. Real
data, the running instance and the outside world are untouched. The container
is removed at the end.

## Run it

On the deployment host, from any directory (about two minutes; it pulls only
what `docker run` needs):

```bash
curl -fsSL https://raw.githubusercontent.com/krzysztofbury/virgil/main/scripts/load_check.py \
  | docker run --rm -i --network none --entrypoint /app/.venv/bin/python \
      ghcr.io/krzysztofbury/virgil:latest - --users 3 --clients 12 --seconds 90
```

It prints a Markdown block to add under "Results" below. Locally:
`uv run --locked python scripts/load_check.py --seconds 20`.

## What it does

- **Reads only**, for the first half: every simulated person opens a page
  (dashboard, Daily, Training, Oura, Blood, Goals, Experiments, Freedom) every
  0.5 to 1.5 seconds.
- **Reads with writes**, for the second half: the reads go on while one person
  per account cycles through saving Daily, uploading a medical PDF (the one
  upload endpoint, 10 MB of synthetic bytes by default), saving again and
  starting a backup, every 2 to 4 seconds.
- **Drain**: it waits until no backup or import job is queued or running.
- A **liveness probe** requests `/livez` four times a second throughout. It does
  no work, so a slow probe means the server as a whole stalled, not one page.

Each simulated person has an address of its own, as behind Cloudflare, so the
per-address rate limit applies per person. The report gives latency per request
kind, status counts, job outcomes and how long jobs took from queued to
finished, and the server process's peak memory.

Limits:

- With no network, a medical import fails at its provider call, so the check
  covers accepting the upload, staging it and running the job, not the
  provider call.
- With network, sending a 10 MB PDF to the provider stalled the server for
  about 0.7 s on a development machine (below).
- The simulated people run in the same container as the server and share its
  CPU.

## Results

### Development machine, 2026-10-21 (reference only)

x86_64, 16 CPUs, the production image `e651759` with `--network none`. 3
accounts, 1.8 MB of databases, 12 simulated readers, 60 s, 10 MB uploads.

| Phase | Request | Count | p50 ms | p95 ms | max ms | Statuses |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Reads | read | 372 | 6 | 82 | 192 | 200: 372 |
| Reads | liveness probe | 119 | 2 | 2 | 4 | 200: 119 |
| Mixed | read | 360 | 6 | 114 | 279 | 200: 360 |
| Mixed | daily save | 16 | 16 | 48 | 144 | 200: 16 |
| Mixed | backup now | 6 | 16 | 18 | 20 | 303: 6 |
| Mixed | upload 10 MB | 9 | 37 | 50 | 64 | 303: 9 |
| Mixed | liveness probe | 118 | 2 | 4 | 89 | 200: 118 |

Jobs:

- Backups all succeeded, a median of 0.7 s from queued to finished.
- Imports failed at the provider call as expected, after 0.9 s.
- The queue was empty when the load stopped.

Server memory peaked at 688 MB, against about 260 MB with reads only. It
stayed near the peak afterwards: Python keeps memory it has used for the next
upload.

Observations from the same machine with network access (a placeholder key, so
the provider refused each request):

- Each 10 MB import stalled the whole server for up to about 0.7 s, presumably
  while LiteLLM built and sent the 13 MB base64 request. The liveness probe
  caught it; reads had a p95 of 611 ms in that phase.
- Without uploads, the same mixed phase showed no stall (probe max 2 ms).
- Each sign-in holds the event loop for about 175 ms while bcrypt checks the
  password.

### Deployment hardware

Pending: run the command above on the NAS and record the block it prints here.
