"""Append-only event log.

Every claim the manifest makes is checkable against trace.jsonl. In particular
a citation is only accepted if the cited message id was actually read during
the run, which means `read` events have to be emitted by the mail store itself
rather than by the code that wants the citation to pass.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class Tracer:
    def __init__(self, path: Path, cap: str | None = None, append: bool = True):
        self.path = Path(path)
        self.run_id = f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.cap = cap
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not append and self.path.exists():
            self.path.unlink()
        self._events: list[dict[str, Any]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "cap": fields.pop("cap", self.cap),
            "event": event,
        }
        record.update(fields)
        self._events.append(record)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def events(self, event: str | None = None) -> list[dict[str, Any]]:
        if event is None:
            return list(self._events)
        return [e for e in self._events if e.get("event") == event]

    def read_ids(self) -> set[str]:
        """Message ids this run actually pulled out of the mail store."""
        return {e["msg_id"] for e in self._events if e.get("event") == "read" and e.get("msg_id")}


_active: Tracer | None = None


def start(path: Path, cap: str | None = None, append: bool = True) -> Tracer:
    global _active
    _active = Tracer(path, cap=cap, append=append)
    return _active


def active() -> Tracer | None:
    return _active


def emit(event: str, **fields: Any) -> dict[str, Any] | None:
    if _active is None:
        return None
    return _active.emit(event, **fields)


def load_trace(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
