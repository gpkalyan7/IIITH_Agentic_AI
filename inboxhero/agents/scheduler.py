"""Meeting requests, checked against what the owner has already said.

This is where a stored preference becomes visible behaviour. m041 states "I do
not take meetings before 11:00am, ever" and is recorded once. On any later run
-- new process, nothing in memory but what is on disk -- m043 arrives proposing
Monday at 9:00am, and the system declines it, offers times that satisfy the
preference, and holds the reply for approval rather than sending it.

The preference was read from prefs.json,
the alternatives are computed here, and the reply still has to pass the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import tracing
from ..mailstore import Message
from ..memory import PreferenceStore
from . import commitments

_PROPOSES = re.compile(
    r"(?i)\b(does .{0,25}work|can you do|could you do|are you free|would .{0,20}suit"
    r"|works? (on|for) your side|available)\b"
)


@dataclass
class SchedulingCheck:
    msg_id: str
    proposed_when: date | None
    proposed_time: str
    minutes: int | None
    violates: str = ""
    preference_key: str = ""
    preference_source: str = ""
    alternatives: list[str] = field(default_factory=list)
    clashes_with: list[str] = field(default_factory=list)
    # Message ids behind the refusal: the preference that forbids the slot and
    # the commitment already occupying it. A decline sent on the owner's behalf
    # should be as traceable as a factual reply.
    cites: list[str] = field(default_factory=list)

    @property
    def needs_pushback(self) -> bool:
        return bool(self.violates or self.clashes_with)

    def to_dict(self) -> dict:
        return {
            "message": self.msg_id,
            "proposed": f"{self.proposed_when} {self.proposed_time}".strip(),
            "violates_preference": self.violates,
            "preference": self.preference_key,
            "preference_from": self.preference_source,
            "clashes_with": self.clashes_with,
            "alternatives_offered": self.alternatives,
        }


def is_scheduling_request(msg: Message) -> bool:
    text = f"{msg.subject} {msg.body}"
    return bool(_PROPOSES.search(text) and commitments._TIME.search(text))


def check(
    msg: Message,
    prefs: PreferenceStore,
    calendar: list[commitments.Commitment] | None = None,
    *,
    cap: str | None = None,
) -> SchedulingCheck:
    text = f"{msg.subject}\n{msg.body}"
    when = commitments._parse_day(text, msg.when.date())
    time_text, minutes = commitments._parse_time(text)
    result = SchedulingCheck(msg.id, when, time_text, minutes)

    for pref in prefs.of_kind("scheduling_window"):
        earliest = _minutes_from(pref.value)
        if earliest is None or minutes is None:
            continue
        if minutes < earliest:
            result.violates = (
                f"{time_text} is before the owner's earliest meeting time of {pref.value}"
            )
            result.preference_key = pref.key
            result.preference_source = pref.source_msg
            result.alternatives = _alternatives(earliest)
            if pref.source_msg:
                result.cites.append(pref.source_msg)
            tracing.emit(
                "preference_applied", cap=cap, msg_id=msg.id, key=pref.key,
                effect="declined a meeting before the owner's earliest time",
                because=pref.source_msg, proposed=time_text, earliest=pref.value,
            )

    for booked in calendar or []:
        if booked.when == when and booked.minutes == minutes and msg.id not in booked.cites:
            result.clashes_with.append(f"{booked.title} [{', '.join(booked.cites)}]")
            result.cites.extend(c for c in booked.cites if c not in result.cites)
            if not result.alternatives:
                result.alternatives = _alternatives(minutes + 60 if minutes else 660)

    return result


def _minutes_from(value: str) -> int | None:
    match = re.match(r"\s*(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _alternatives(earliest_minutes: int, count: int = 3) -> list[str]:
    """Three concrete times that satisfy the constraint, an hour apart."""
    out = []
    for index in range(count):
        total = earliest_minutes + index * 60
        out.append(f"{total // 60:02d}:{total % 60:02d}")
    return out


def compose_pushback(msg: Message, check: SchedulingCheck) -> str:
    lines = ["Hi,", ""]
    if check.violates:
        lines.append(
            f"Thanks for the suggestion. {check.proposed_time} does not work -- "
            f"I do not take meetings that early."
        )
    if check.clashes_with:
        lines.append(
            f"That slot is already taken by: {'; '.join(check.clashes_with)}."
        )
    if check.alternatives:
        lines.append("")
        lines.append("Any of these would work instead:")
        for slot in check.alternatives:
            lines.append(f"  - {slot}")
    lines += ["", "Best,", "Sam"]
    return "\n".join(lines)
