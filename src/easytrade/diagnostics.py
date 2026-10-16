"""Private local console action journal. No network calls or account operations."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from .domain import utc_now

CONSOLE_BUILD = "20261016.11"
_write_lock = threading.Lock()


def record_action(root, event, **fields):
    path = Path(root) / "runtime/console-actions.jsonl"
    payload = {"at": utc_now().isoformat(), "build": CONSOLE_BUILD, "event": event, **fields}
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
