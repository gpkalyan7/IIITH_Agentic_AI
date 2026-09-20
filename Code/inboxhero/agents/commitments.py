"""Dates, deadlines and obligations, resolved to a calendar.

Three things this has to do beyond finding dates.

*Cite.* Every commitment records the message ids it came from, and those ids
are checked against the trace the same way a draft's citations are.

*Resolve across messages.* Some commitments do not exist in any single
message. The board deck is the example: m040 says "finished and circulated two
days before the board review" and never says when that is, while m038 says the
review is on the 18th and never mentions the deck. Neither message contains
the commitment; it only exists once both are read, and it resolves to the
16th, citing both.

*Surface conflicts, not just list them.* Two things at the same time is a
finding, not a row. It is reported separately and loudly.

Dates in this mailbox are bare day-of-month ("the 18th", "Tuesday the 15th")
against timestamps in September 2026, so they are resolved into that month.
The assumption is recorded in the manifest rather than hidden here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .. import tracing
from ..mailstore import MailStore, Message

# The mailbox runs through September 2026; every bare day-of-month lands there.
BASE_YEAR = 2026
BASE_MONTH = 9

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_DAY_ORDINAL = re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_MONTH_DAY = re.compile(r"\b(?:sep|sept|september)\s+(\d{1,2})\b", re.IGNORECASE)
_WEEKDAY = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_RELATIVE_DAYS = re.compile(r"\b(?:(\w+)\s+)?days? (?:before|ahead of|prior to)\b", re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "a": 1, "": 1}


@dataclass
class Commitment:
    title: str
    when: date | None
    time_text: str
    cites: list[str]
    kind: str  # "meeting" | "deadline" | "renewal" | "appointment"
    detail: str = ""
    derived: bool = False  # True when no single message contained it
    minutes: int | None = None  # minute-of-day, for conflict detection

    @property
    def when_text(self) -> str:
        if self.when is None:
            return "date unresolved"
        stamp = self.when.strftime("%a %d %b")
        return f"{stamp} {self.time_text}".strip()

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "date": self.when.isoformat() if self.when else None,
            "time": self.time_text,
            "when": self.when_text,
            "kind": self.kind,
            "cites": self.cites,
            "derived_from_multiple": self.derived,
            "detail": self.detail,
        }


@dataclass
class Conflict:
    when_text: str
    items: list[Commitment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "when": self.when_text,
            "items": [
                {"title": c.title, "cites": c.cites} for c in self.items
            ],
            "note": "two commitments at the same time; the owner must drop or move one",
        }


# -- parsing ----------------------------------------------------------------


def _parse_day(text: str, sent_on: date) -> date | None:
    match = _MONTH_DAY.search(text)
    if match:
        return _safe_date(int(match.group(1)))
    match = _DAY_ORDINAL.search(text)
    if match:
        return _safe_date(int(match.group(1)))
    weekdays = list(_WEEKDAY.finditer(text))
    if weekdays:
        # Which weekday is the message actually proposing? m013 names three:
        # "move our 1:1 from Thursday to Wednesday at 2:00pm this week...
        # something came up Thursday morning". The one that matters is the one
        # carrying the time, so prefer the weekday immediately before a time
        # expression and fall back to the last one named.
        chosen = weekdays[-1]
        time_match = _TIME.search(text)
        if time_match:
            before_time = [w for w in weekdays if w.start() < time_match.start()]
            if before_time:
                chosen = before_time[-1]
        target = _WEEKDAYS[chosen.group(1).lower()]
        ahead = (target - sent_on.weekday()) % 7
        if "next week" in text.lower():
            ahead += 7
        # A weekday named in a message sent on that same weekday means the
        # next one, unless the message says "this week".
        elif ahead == 0:
            ahead = 0 if "this week" in text.lower() else 7
        return sent_on + timedelta(days=ahead)
    return None


def _safe_date(day: int) -> date | None:
    try:
        return date(BASE_YEAR, BASE_MONTH, day)
    except ValueError:
        return None


def _parse_time(text: str) -> tuple[str, int | None]:
    match = _TIME.search(text)
    if not match:
        return "", None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm":
        hour += 12
    return f"{hour:02d}:{minute:02d}", hour * 60 + minute


# -- extraction -------------------------------------------------------------

# Order matters: the most specific phrasing wins. "board deck" has to be
# tested before "board review", because m040 mentions both and the commitment
# it carries is the deck.
_TOPIC_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("deadline", re.compile(r"(?i)board deck"), "Board deck finished and circulated"),
    ("deadline", re.compile(r"(?i)board minutes"), "Flag corrections to draft board minutes"),
    ("meeting", re.compile(r"(?i)board review"), "Quarterly board review"),
    ("meeting", re.compile(r"(?i)intro call|hear the vision"), "Intro call with Northwind VC"),
    ("meeting", re.compile(r"(?i)one more slot|partner wants to join"), "Northwind partner call"),
    ("meeting", re.compile(r"(?i)product demo|demo on"), "Product demo for Acme"),
    ("meeting", re.compile(r"(?i)1:1"), "Weekly 1:1 with Raghav"),
    ("appointment", re.compile(r"(?i)dental cleaning|your appointment"), "Dental cleaning"),
    ("deadline", re.compile(r"(?i)pricing (page )?copy"), "Approve final pricing page copy"),
    ("deadline", re.compile(r"(?i)load test"), "Load test on signup flow"),
    ("deadline", re.compile(r"(?i)safe amendment|clause 4"), "Sign the SAFE amendment"),
    ("deadline", re.compile(r"(?i)ip assignment"), "Sign the IP assignment"),
    ("deadline", re.compile(r"(?i)another offer|offer i need to respond"), "Candidate's competing offer expires"),
    ("deadline", re.compile(r"(?i)timesheet"), "Submit timesheet"),
    ("deadline", re.compile(r"(?i)launch coverage|on deadline for"), "Press deadline for launch coverage"),
    ("deadline", re.compile(r"(?i)product hunt copy|email blast"), "Product Hunt copy and email blast draft"),
    ("deadline", re.compile(r"(?i)hero image|design assets"), "Final design assets"),
    ("deadline", re.compile(r"(?i)the 20th is a hard date|target is the 20th"), "Launch day"),
    ("deadline", re.compile(r"(?i)hold expires"), "Venue hold expires"),
    ("deadline", re.compile(r"(?i)\bpto\b|i'?m out"), "Devika on PTO"),
    ("deadline", re.compile(r"(?i)office (will be )?closed"), "Office closed for maintenance"),
    ("renewal", re.compile(r"(?i)renews?\b"), "Subscription renewal"),
]


def _title_for(msg: Message) -> tuple[str, str]:
    """Subject first: it is the author's own summary of what the message is about."""
    for source in (msg.subject, msg.body):
        for kind, pattern, title in _TOPIC_PATTERNS:
            if pattern.search(source):
                return kind, title
    return "deadline", msg.subject.replace("Re: ", "").strip() or "Untitled commitment"


def extract(store: MailStore, messages: list[Message], *, cap: str | None = None) -> tuple[list[Commitment], list[Conflict]]:
    found: list[Commitment] = []

    for msg in messages:
        text = f"{msg.subject}\n{msg.body}"
        when = _parse_day(text, msg.when.date())
        time_text, minutes = _parse_time(text)
        # A commitment with no date is not a calendar entry. Preference notes
        # and general chatter fall out here rather than cluttering the pane.
        if when is None:
            continue
        kind, title = _title_for(msg)
        found.append(
            Commitment(
                title=title,
                when=when,
                time_text=time_text,
                cites=[msg.id],
                kind=kind,
                detail=msg.preview(90),
                minutes=minutes,
            )
        )

    found += _derive_relative(store, messages, found, cap=cap)
    found = _merge_duplicates(found)
    found.sort(key=lambda c: (c.when or date(BASE_YEAR, 12, 31), c.minutes if c.minutes is not None else 0))

    for commitment in found:
        tracing.emit(
            "commitment", cap=cap, title=commitment.title,
            when=commitment.when.isoformat() if commitment.when else None,
            cites=commitment.cites, derived=commitment.derived,
        )

    return found, _conflicts(found)


def _derive_relative(
    store: MailStore, messages: list[Message], existing: list[Commitment], *, cap: str | None = None
) -> list[Commitment]:
    """Commitments expressed relative to an event described in another message.

    m040 says the deck is due "two days before the board review" without
    saying when that is. m038 fixes the review to the 18th without mentioning
    the deck. Only reading both yields "16 Sep", citing both.
    """
    derived: list[Commitment] = []
    for msg in messages:
        match = _RELATIVE_DAYS.search(msg.body)
        if not match:
            continue
        offset = _NUMBER_WORDS.get((match.group(1) or "").lower())
        if offset is None:
            try:
                offset = int(match.group(1))
            except (TypeError, ValueError):
                continue

        anchor_terms = _anchor_terms(msg.body, match.start())
        anchor = _find_anchor(store, anchor_terms, exclude=msg.id)
        if anchor is None or anchor.when is None:
            continue

        kind, title = _title_for(msg)
        derived.append(
            Commitment(
                title=title,
                when=anchor.when - timedelta(days=offset),
                time_text="",
                cites=sorted({msg.id, *anchor.cites}),
                kind=kind,
                detail=(
                    f"{offset} days before {anchor.title.lower()} on "
                    f"{anchor.when.strftime('%d %b')}; neither message states this date alone"
                ),
                derived=True,
            )
        )
        tracing.emit(
            "commitment_derived", cap=cap, title=title,
            from_msgs=sorted({msg.id, *anchor.cites}),
            resolved_to=(anchor.when - timedelta(days=offset)).isoformat(),
        )
    return derived


def _anchor_terms(body: str, upto: int) -> list[str]:
    """The event name sitting just after 'N days before ...'."""
    tail = body[upto:upto + 60].lower()
    words = re.findall(r"[a-z]{4,}", tail)
    return [w for w in words if w not in ("days", "before", "ahead", "prior")][:3]


def _find_anchor(store: MailStore, terms: list[str], *, exclude: str) -> Commitment | None:
    if not terms:
        return None
    for candidate in store.search(terms, exclude={exclude}, limit=3):
        msg = store.get(candidate.id, reason="resolving a relative date")
        when = _parse_day(f"{msg.subject} {msg.body}", msg.when.date())
        if when is None:
            continue
        kind, title = _title_for(msg)
        time_text, minutes = _parse_time(msg.body)
        return Commitment(title, when, time_text, [msg.id], kind, minutes=minutes)
    return None


def _merge_duplicates(items: list[Commitment]) -> list[Commitment]:
    """The same obligation mentioned in several messages is one entry, citing all."""
    merged: dict[tuple, Commitment] = {}
    for item in items:
        key = (item.title, item.when, item.time_text)
        if key in merged:
            existing = merged[key]
            existing.cites = sorted(set(existing.cites) | set(item.cites))
            existing.derived = existing.derived or item.derived or len(existing.cites) > 1
        else:
            merged[key] = item
    return list(merged.values())


def _conflicts(items: list[Commitment]) -> list[Conflict]:
    buckets: dict[tuple, list[Commitment]] = {}
    for item in items:
        if item.when is None or item.minutes is None:
            continue
        buckets.setdefault((item.when, item.minutes), []).append(item)
    conflicts = []
    for (when, _), group in sorted(buckets.items()):
        if len(group) > 1:
            conflict = Conflict(when_text=group[0].when_text, items=group)
            conflicts.append(conflict)
            tracing.emit(
                "conflict", when=when.isoformat(),
                titles=[c.title for c in group],
                cites=sorted({cite for c in group for cite in c.cites}),
            )
    return conflicts
