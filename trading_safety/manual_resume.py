"""Atomic request/result files for operator-approved critical-pause resume."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

from .order_state import atomic_write_json


# Operator confirmation phrase, not an authentication credential.
CONFIRM_TOKEN = "I_CONFIRM_MANUAL_RISK_CHECK_DONE"  # nosec B105
REQUEST_FILE = Path("runtime/manual_resume_request.json")
RESULT_FILE = Path("runtime/manual_resume_result.json")
ARCHIVE_DIR = Path("runtime/manual_resume_archive")


@dataclass(frozen=True)
class ManualResumeRequest:
    request_id: str
    symbol: str
    requested_at: str
    requested_by: str


@dataclass(frozen=True)
class ManualResumeResult:
    request_id: str
    approved: bool
    status: str
    issues: tuple[str, ...]
    reconciled_at: str | None


def write_manual_resume_request(
    symbol: str,
    confirm_token: str,
    *,
    requested_by: str,
    path: str | Path = REQUEST_FILE,
) -> ManualResumeRequest:
    if confirm_token != CONFIRM_TOKEN:
        raise ValueError("Manual confirmation text mismatch")
    request = ManualResumeRequest(
        request_id=str(uuid.uuid4()),
        symbol=str(symbol),
        requested_at=datetime.now(timezone.utc).isoformat(),
        requested_by=str(requested_by or "unknown"),
    )
    payload = asdict(request)
    payload["confirm_token"] = confirm_token
    atomic_write_json(path, payload, indent=2)
    return request


def load_manual_resume_request(
    path: str | Path = REQUEST_FILE,
) -> ManualResumeRequest | None:
    target = Path(path)
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("confirm_token") != CONFIRM_TOKEN:
        raise ValueError("Manual resume request confirmation token is invalid")
    return ManualResumeRequest(
        request_id=str(payload["request_id"]),
        symbol=str(payload["symbol"]),
        requested_at=str(payload["requested_at"]),
        requested_by=str(payload.get("requested_by") or "unknown"),
    )


def write_manual_resume_result(
    result: ManualResumeResult,
    path: str | Path = RESULT_FILE,
) -> None:
    atomic_write_json(path, asdict(result), indent=2)


def archive_processed_request(
    request: ManualResumeRequest,
    path: str | Path = REQUEST_FILE,
) -> Path | None:
    target = Path(path)
    if not target.exists():
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive = ARCHIVE_DIR / f"request_{request.request_id}.json"
    target.replace(archive)
    return archive


def archive_critical_pause(
    pause_path: str | Path,
    request: ManualResumeRequest,
) -> Path:
    target = Path(pause_path)
    if not target.exists():
        raise FileNotFoundError(target)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = request.symbol.replace("/", "_").replace(":", "_")
    archive = ARCHIVE_DIR / f"critical_pause_{safe_symbol}_{request.request_id}.json"
    shutil.move(str(target), str(archive))
    return archive
