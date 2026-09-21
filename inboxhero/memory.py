"""Standing preferences that outlive a run.

Adapted from the JSON-file memory written for Assignment 5: same last-write-
wins-per-key model, same "survives a restart because it is on disk" property.
Two things are new, and both exist because this store is now reachable from
text that strangers wrote.

**The gate never reads this file.** Even a preference that somehow got written
could not loosen a control, because gate.py has no reference to this module.
Preferences shape *what* the system proposes. They have no say in *whether* a
proposal needs a human.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import tracing

# The closed vocabulary. Adding a kind here is a deliberate act by the system's
# author; it is not something a message can do.
KINDS = {
    "scheduling_window": "earliest/latest time of day the owner will take meetings",
    "always_cc": "an address to copy on mail matching a correspondent or topic",
    "never_agree": "something the owner does not consent to",
    "tone": "how to address a particular correspondent",
    "priority_sender": "a correspondent whose mail is surfaced first",
}

# Anything a stored preference must never be able to influence.
FORBIDDEN_EFFECTS = (
    "approval",
    "gate",
    "autonomous",
    "auto-send",
    "confirmation",
    "delete",
    "conceal",
)


class PreferenceRejected(ValueError):
    """A proposed preference is outside what this store can represent."""


@dataclass
class Preference:
    key: str
    kind: str
    value: str
    source_msg: str
    stated_by: str
    recorded_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # Free-text description for humans and the dashboard.
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreferenceStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._prefs: dict[str, Preference] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for key, item in raw.items():
            # Re-validate on read. A file edited by hand, or written by an
            # older version, cannot smuggle in a kind we no longer allow.
            if item.get("kind") not in KINDS:
                continue
            self._prefs[key] = Preference(
                key=key,
                kind=item["kind"],
                value=item.get("value", ""),
                source_msg=item.get("source_msg", ""),
                stated_by=item.get("stated_by", ""),
                recorded_at=item.get("recorded_at", ""),
                description=item.get("description", ""),
            )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: pref.to_dict() for key, pref in self._prefs.items()}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- writing ---------------------------------------------------------

    def remember(
        self, *, key: str, kind: str, value: str, source_msg: str, stated_by: str, description: str = ""
    ) -> Preference:
        """Store a preference. Raises PreferenceRejected if it is not representable."""
        if kind not in KINDS:
            raise PreferenceRejected(
                f"'{kind}' is not a preference this system can hold; allowed kinds are {sorted(KINDS)}"
            )
        haystack = f"{key} {value} {description}".lower()
        for word in FORBIDDEN_EFFECTS:
            if word in haystack:
                raise PreferenceRejected(
                    f"a preference may not mention '{word}': preferences shape what is "
                    f"proposed, never whether a proposal needs a human"
                )
        pref = Preference(
            key=key,
            kind=kind,
            value=value,
            source_msg=source_msg,
            stated_by=stated_by,
            description=description or value,
        )
        self._prefs[key] = pref  # last write wins, as in Assignment 5
        self._save()
        tracing.emit(
            "preference_stored", key=key, kind=kind, value=value, msg_id=source_msg, stated_by=stated_by
        )
        return pref

    def forget(self, key: str) -> bool:
        if key in self._prefs:
            del self._prefs[key]
            self._save()
            return True
        return False

    def clear(self) -> None:
        self._prefs.clear()
        if self.path.exists():
            self.path.unlink()

    # -- reading ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._prefs)

    def all(self) -> list[Preference]:
        return sorted(self._prefs.values(), key=lambda p: p.key)

    def get(self, key: str) -> Preference | None:
        return self._prefs.get(key)

    def of_kind(self, kind: str) -> list[Preference]:
        return [p for p in self._prefs.values() if p.kind == kind]

    def summary(self) -> list[str]:
        return [f"{p.key}: {p.description} (from {p.source_msg})" for p in self.all()]
