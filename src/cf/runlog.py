import json
import uuid
from datetime import datetime, timezone

# skipped — прогон не состоялся осознанно (конвейер занят, лок не взят):
# не сбой и не успех, но след в Run Log обязателен (железное правило №6).
VALID_STATUSES = {"success", "failed", "insufficient_data", "skipped"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_run(sheets, agent, status, input_summary="", output_paths=(), errors=(),
            trigger_type="manual", started_at=None, run_id=None):
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
    completed = now_iso()
    row = {
        "run_id": run_id or uuid.uuid4().hex[:12],
        "agent": agent,
        "trigger_type": trigger_type,
        "started_at": started_at or completed,
        "completed_at": completed,
        "status": status,
        "input_summary": input_summary,
        "output_paths": json.dumps(list(output_paths), ensure_ascii=False),
        "errors": json.dumps(list(errors), ensure_ascii=False),
    }
    sheets.append_row("run_log", row)
    return row
