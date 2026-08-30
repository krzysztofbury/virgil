"""Medical record import: stage the upload, extract markers, store them.

The upload never travels inside a job payload. Payloads are bounded and land in
the jobs table, the status UI and the logs; a blood panel belongs in none of
those. The payload carries an opaque token, and the bytes wait in a per-user
staging directory that the handler deletes once the markers are stored.
"""

import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from app.config import CENTRAL_DB_PATH, INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

logger = logging.getLogger(__name__)

STAGING_DIR = Path(CENTRAL_DB_PATH).parent / "staging"
STAGED_MAX_AGE_SECONDS = 24 * 3600
MEDICAL_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
MEDICAL_TEXT_MAX_CHARS = 10000
MAX_MARKERS = 200
MAX_RESULTS_PER_MARKER = 50
MEDICAL_SOURCES = frozenset({"pdf", "text"})

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_PDF_PROMPT = (
    "Extract all blood test markers from this PDF. "
    "Return ONLY a markdown list, one marker per line, format: "
    "'### Marker Name\\n* YYYY-MM-DD: value unit (flag)'. "
    "Include all dates found. Use (H) for high, (L) for low flags."
)
_MARKER_PROMPT = (
    "Extract blood test markers from the text. Return JSON array of objects: "
    '{"marker": "name", "unit": "unit", "ref_low": number_or_null, "ref_high": number_or_null, '
    '"results": [{"date": "YYYY-MM-DD", "value": number, "flag": "H"/"L"/null}]}. '
    "Return ONLY valid JSON, no markdown fences."
)


def _user_dir(user_id: str) -> Path:
    if not _USER_RE.fullmatch(user_id or ""):
        raise ValueError("Staging requires a simple user identifier")
    return STAGING_DIR / user_id


def staged_path(user_id: str, token: str) -> Path:
    """Resolve one staged upload. The token is opaque and the directory is the
    user's own, so a tampered payload cannot reach another account's file."""
    if not _TOKEN_RE.fullmatch(token or ""):
        raise ValueError("Staged upload token must be 32 lowercase hexadecimal characters")
    return _user_dir(user_id) / f"{token}.bin"


def stage_upload(user_id: str, data: bytes) -> str:
    if not isinstance(data, bytes) or not data:
        raise ValueError("A staged upload cannot be empty")
    if len(data) > MEDICAL_UPLOAD_MAX_BYTES:
        raise ValueError("A staged upload is larger than the accepted maximum")
    directory = _user_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    staged_path(user_id, token).write_bytes(data)
    return token


def read_staged(user_id: str, token: str) -> bytes:
    path = staged_path(user_id, token)
    if not path.is_file():
        raise ValueError("The staged medical upload is no longer available")
    return path.read_bytes()


def discard_staged(user_id: str, token: str) -> None:
    staged_path(user_id, token).unlink(missing_ok=True)


def prune_staged(user_id: str, *, max_age_seconds: int = STAGED_MAX_AGE_SECONDS) -> int:
    """Drop uploads whose job never ran. Medical bytes must not linger."""
    directory = _user_dir(user_id)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in directory.glob("*.bin"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            logger.warning("Could not prune a staged medical upload")
    return removed


async def extract_pdf_markers(pdf_bytes: bytes) -> str:
    """Turn a blood-panel PDF into markdown. One paid multimodal call."""
    import base64

    import litellm

    if not INTERNAL_LLM_KEY:
        raise ValueError("Medical record import requires an internal LLM key")
    encoded = base64.b64encode(pdf_bytes).decode()
    try:
        response = await litellm.acompletion(
            model=INTERNAL_LLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PDF_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:application/pdf;base64,{encoded}"}},
                    ],
                }
            ],
            api_key=INTERNAL_LLM_KEY,
            max_tokens=4096,
            timeout=120.0,
        )
    except (litellm.Timeout, litellm.APIError) as exc:
        # Same reasoning as call_llm: transport failure does not prove the
        # provider refused the request, and this one is multimodal and costly.
        from app.services.llm import LLMCallAmbiguousError

        raise LLMCallAmbiguousError(f"Medical PDF extraction outcome is uncertain: {exc}") from exc
    if not response.choices or not response.choices[0].message.content:
        raise ValueError("Medical PDF extraction returned no content")
    return response.choices[0].message.content


async def parse_medical_text(text: str) -> list[dict[str, Any]]:
    """Turn free text into marker records. One paid call, no database writes."""
    import litellm

    if not INTERNAL_LLM_KEY:
        raise ValueError("Medical record import requires an internal LLM key")
    try:
        response = await litellm.acompletion(
            model=INTERNAL_LLM_MODEL,
            messages=[
                {"role": "system", "content": _MARKER_PROMPT},
                {"role": "user", "content": text},
            ],
            api_key=INTERNAL_LLM_KEY,
            max_tokens=4096,
            timeout=90.0,
        )
    except (litellm.Timeout, litellm.APIError) as exc:
        from app.services.llm import LLMCallAmbiguousError

        raise LLMCallAmbiguousError(f"Medical marker extraction outcome is uncertain: {exc}") from exc

    if not response.choices or not response.choices[0].message.content:
        return []
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    try:
        markers = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse medical markers JSON")
        return []
    if not isinstance(markers, list):
        return []
    return markers[:MAX_MARKERS]


async def save_medical_markers(db, markers: list[dict[str, Any]]) -> int:
    """Store markers and their results. The caller owns the transaction."""
    imported = 0
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        name = marker.get("marker", "")
        if not name:
            continue
        await db.execute(
            """INSERT INTO blood_markers (name, unit, ref_low, ref_high, category)
               VALUES (?, ?, ?, ?, 'Imported')
               ON CONFLICT(name) DO UPDATE SET
                   unit = COALESCE(excluded.unit, unit),
                   ref_low = COALESCE(excluded.ref_low, ref_low),
                   ref_high = COALESCE(excluded.ref_high, ref_high)""",
            (name, marker.get("unit", ""), marker.get("ref_low"), marker.get("ref_high")),
        )
        marker_row = await db.execute_fetchall("SELECT id FROM blood_markers WHERE name = ?", (name,))
        if not marker_row:
            continue
        imported += 1
        marker_id = marker_row[0]["id"]

        results = marker.get("results", [])
        if not isinstance(results, list):
            continue
        for result in results[:MAX_RESULTS_PER_MARKER]:
            if not isinstance(result, dict):
                continue
            date_val = result.get("date", "")
            value = result.get("value")
            if date_val and value is not None:
                await db.execute(
                    """INSERT INTO blood_results (marker_id, date, value, flag)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    (marker_id, date_val, value, result.get("flag") or ""),
                )
    return imported
