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
- A **stand-in provider** on loopback receives each import's request, reads it
  whole and refuses the placeholder key, as the real provider would. Imports
  therefore fail, but only after building, sending and handling the request.

Each simulated person has an address of its own, as behind Cloudflare, so the
per-address rate limit applies per person. The report gives latency per request
kind, status counts, job outcomes and how long jobs took from queued to
finished, and the server process's peak memory.

Limits:

- No real extraction runs: the stand-in refuses every request. The runs
  recorded below, from before the stand-in existed, stopped at a failed DNS
  lookup instead, which missed the stall that #122 fixed.
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

### Deployment hardware, 2026-10-21

The NAS: x86_64, 4 CPUs, the production image `e651759` with `--network none`.
3 accounts, 1.8 MB of databases (seeded in 14 s), 12 simulated readers, 90 s,
10 MB uploads. The simulated people share those 4 CPUs with the server, and
generating and encoding each 10 MB upload is their work too, so the times are
somewhat worse than real visitors would see.

| Phase | Request | Count | p50 ms | p95 ms | max ms | Statuses |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Reads | read | 511 | 40 | 317 | 1732 | 200: 511 |
| Reads | liveness probe | 168 | 7 | 47 | 260 | 200: 168 |
| Mixed | read | 437 | 71 | 1040 | 2116 | 200: 437 |
| Mixed | daily save | 21 | 208 | 554 | 984 | 200: 21 |
| Mixed | backup now | 9 | 198 | 285 | 463 | 303: 9 |
| Mixed | upload 10 MB | 9 | 820 | 2261 | 2503 | 303: 9 |
| Mixed | liveness probe | 151 | 8 | 216 | 389 | 200: 151 |

Jobs:

- All 9 backups succeeded, a median of 1.4 s from queued to finished (max 1.7 s).
- All 9 imports failed at the provider call as expected (no network), after
  1.9 s (max 2.7 s).
- The queue was empty when the load stopped.

Server memory peaked at 723 MB during the uploads and was 384 MB afterwards.
The server log holds only the expected connection errors of the imports.

What this shows:

- **Correctness:** nothing failed. Every request was answered with its
  expected status, every backup succeeded, and no job was left behind, at 12
  simultaneous readers, several times what the household produces.
- **Reads:** a page takes about 40 ms; the slowest 5% take about 0.3 s, and
  about 1 s while uploads and jobs run.
- **Stalls:** the liveness probe stayed under 0.4 s throughout. The offline run
  cannot include the provider call, which stalled the server for about 0.7 s
  per 10 MB import on the development machine; this NAS is several times
  slower, so expect seconds.
- **Memory:** peaks during uploads, at about three times the reads-only figure,
  so the host needs that headroom.

Follow-ups were in #122.

### After #122, development machine

#122 found the stall. The provider's refusal was not slow, but logging it was:
the MCP SDK had installed Rich's log handler, which rendered each traceback with
syntax highlighting on the event loop. Logging one failed import took 0.9 s.
With plain log lines it takes 0.02 s.

Against a refusing stand-in provider, 2 accounts, 6 readers, 40 s, 10 MB uploads:

- The liveness probe's worst time while imports failed fell from 860 ms to
  129 ms.
- Reads in that phase fell from a p95 of 733 ms to 37 ms.
- Peak memory fell from 510 MB to 426 MB.

Sign-in now checks the password in a thread, which leaves other requests
running for those 0.2 s.

### After #122, deployment hardware, 2026-10-22

The same NAS and command as on 2026-10-21, with the image `12722d3`. The
imports now fail against the load check's refusing stand-in provider, the case
that stalled the server before #122.

| Phase | Request | Count | p50 ms | p95 ms | max ms | Statuses |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Reads | read | 518 | 35 | 251 | 2050 | 200: 518 |
| Reads | liveness probe | 172 | 6 | 23 | 174 | 200: 172 |
| Mixed | read | 470 | 61 | 671 | 1442 | 200: 470 |
| Mixed | daily save | 20 | 195 | 861 | 876 | 200: 20 |
| Mixed | backup now | 9 | 145 | 207 | 224 | 303: 9 |
| Mixed | upload 10 MB | 10 | 610 | 1812 | 1958 | 303: 10 |
| Mixed | liveness probe | 160 | 8 | 139 | 343 | 200: 160 |

Jobs: all 9 backups succeeded, a median of 1.0 s from queued to finished (max
1.5 s). All 10 imports failed at the provider as expected, after 1.6 s (max
3.3 s). The queue was empty when the load stopped. The server log holds only
those refusals.

Compared with 2026-10-21:

- **Stalls:** while imports failed, the liveness probe's p95 fell from 216 ms
  to 139 ms and its worst time from 389 ms to 343 ms, now with the provider
  call included. Reads in that phase fell from a p95 of 1040 ms to 671 ms.
- **Memory:** the peak during uploads fell from 723 MB to 570 MB (high-water
  585 MB) and was 492 MB afterwards.

The container gets `mem_limit: 1g` in docker-compose.yml: about 1.7 times the
high-water mark, measured at several times the household's load. A server
that grows past it is restarted rather than taking memory from the rest of
the NAS.
