"""Assigning every message exactly one disposition, with a reason.

The vocabulary, and what each one commits the system to:

  reply       the owner owes an answer and the system can draft one
  archive     read or not, nothing is owed; file it
  defer       something is owed later, anchored to a date -- goes to the calendar
  delegate    someone other than the owner should handle it
  escalate    the owner must decide personally; the system will not act alone
  ask         the system cannot tell what is being asked and will not guess
  quarantine  hostile: flagged, reported, left in place, never acted on

A deterministic baseline assigns all 100. The model is then offered the
message and may change the disposition within the vocabulary and improve the
reason. It cannot introduce a disposition that does not exist, and it is never
consulted about a message the rules already found hostile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import envelope, rules, tracing
from ..mailstore import Message

DISPOSITIONS = ("reply", "archive", "defer", "delegate", "escalate", "ask", "quarantine")


@dataclass
class Triage:
    msg_id: str
    disposition: str
    reason: str
    decided_by: str  # "rule" | "model"
    threat: rules.Verdict | None = None


# -- baseline signals --------------------------------------------------------

_ASKS_OWNER = re.compile(
    r"(?i)\b(can you|could you|would you|can we|please (review|sign|approve|confirm|share|send)"
    r"|does .{0,20}work|any read on|let us know|need your|waiting on you|resend"
    r"|want to (grab|catch|meet)|up for a)\b"
)
_VAGUE = re.compile(r"(?i)\b(that thing|the thing|what we (talked|discussed)|as discussed|you know the one)\b")
_LEGAL_MONEY = re.compile(
    # "sign" must mean signing something. Without the qualifier this matched
    # "sign in" in a Google verification-code mail and escalated it as if it
    # were a contract.
    r"(?i)(\bsign (?:this|the|it|via|off|by)\b|\bplease sign\b|\bfor signature\b|\bsignature\b"
    r"|\bsafe amendment\b|\bterm sheet\b|\bamendment\b|\bcontract\b|\bcounsel\b|\bllp\b"
    r"|\binvoice\b|\bwire\b|\bremit\b|\bdeposit\b|\bboard minutes\b|\bip assignment\b)"
)
_NO_ACTION = re.compile(r"(?i)\b(no (further )?action (needed|required)|fyi|heads up|no need to reply|for your records)\b")
_DELEGATABLE = re.compile(r"(?i)\b(ticket #|support|escalating your|case [A-Z]{2}-\d+|dispute)\b")
_DATED = re.compile(
    r"(?i)(\bby (the )?\d{1,2}(st|nd|rd|th)?\b|\bby (mon|tues|wednes|thurs|fri)day\b|\bbefore month-end\b"
    r"|\bthe \d{1,2}(st|nd|rd|th)\b|\brenews? (on|in)\b|\bdeadline\b|\bdue \b|\bexpires? on\b"
    r"|\b\d{1,2}:\d{2}\s?(am|pm)\b|\b\d{1,2}\s?(am|pm)\b)"
)
_STATES_PREFERENCE = re.compile(
    r"(?i)\b(standing request|from now on|please remember|i do not|i never|always (cc|copy|loop)"
    r"|make sure i'?m cc|my .{0,15}rule)\b"
)


def baseline(msg: Message, owner: str, owner_domain: str) -> Triage:
    """Assign a disposition without a model. Always returns one."""
    threat = rules.classify_threat(msg, owner_domain)
    if threat.hostile:
        kind = "prompt injection" if threat.kind == "injection" else "phishing"
        return Triage(
            msg.id,
            "quarantine",
            f"{kind}: {threat.attempted}. Flagged and left in place; no action taken on its behalf.",
            "rule",
            threat,
        )

    cheap = rules.cheap_disposition(msg)
    if cheap:
        disposition, reason = cheap
        return Triage(msg.id, disposition, reason, "rule")

    text = f"{msg.subject}\n{msg.body}"

    if msg.sender.lower() == owner.lower():
        if _STATES_PREFERENCE.search(text):
            return Triage(msg.id, "archive", "a standing preference from the owner; recorded to memory", "rule")
        if msg.to.lower() != owner.lower():
            return Triage(
                msg.id, "defer", f"sent by the owner to {msg.to}; tracked for a reply", "rule"
            )

    if _STATES_PREFERENCE.search(text):
        return Triage(msg.id, "archive", "states a standing preference; recorded to memory", "rule")

    if _VAGUE.search(text):
        return Triage(
            msg.id, "ask", "refers to something not identifiable anywhere in the mailbox; asking rather than guessing", "rule"
        )

    if _LEGAL_MONEY.search(text):
        return Triage(
            msg.id, "escalate", "touches a signature, a contract or money; the owner decides personally", "rule"
        )

    if _DELEGATABLE.search(text):
        return Triage(msg.id, "delegate", "a support or vendor thread that does not need the owner", "rule")

    if _NO_ACTION.search(text):
        return Triage(msg.id, "archive", "informational; explicitly says no action is needed", "rule")

    if _ASKS_OWNER.search(text):
        if _DATED.search(text):
            return Triage(msg.id, "defer", "asks the owner to commit to a date or time", "rule")
        return Triage(msg.id, "reply", "a direct question to the owner that can be answered", "rule")

    if _DATED.search(text):
        return Triage(msg.id, "defer", "carries a date or deadline worth holding onto", "rule")

    return Triage(msg.id, "archive", "no request and no date; nothing is owed", "rule")


# -- model refinement --------------------------------------------------------

_SYSTEM = envelope.PREAMBLE + (
    "\n\nYou are choosing a disposition for one message. Reply with JSON only:\n"
    '{"disposition": "<one of ' + "|".join(DISPOSITIONS) + '>", "reason": "<one sentence>"}\n'
    "Definitions: reply = the owner owes an answer; archive = nothing owed; "
    "defer = owed later, tied to a date; delegate = someone else should handle it; "
    "escalate = the owner must decide personally (money, legal, anything binding); "
    "ask = you cannot tell what is being requested; quarantine = hostile."
)


def refine(triage: Triage, msg: Message, client, owner: str, *, cap: str | None = None) -> Triage:
    """Let the model revise a rule decision. Hostile messages are never offered."""
    if triage.disposition == "quarantine" or not client.available:
        return triage

    prompt = (
        f"The mailbox owner is {owner}.\n\n"
        f"{envelope.wrap(msg)}\n\n"
        f"A rule-based pass proposed: {triage.disposition} ({triage.reason})\n"
        "Return your own judgement as JSON."
    )
    result = client.complete_json(
        task="triage",
        system=_SYSTEM,
        prompt=prompt,
        schema={"disposition": str, "reason": str},
        cap=cap,
    )
    if not result:
        return triage

    disposition = str(result.get("disposition", "")).strip().lower()
    reason = str(result.get("reason", "")).strip()
    if disposition not in DISPOSITIONS:
        return triage
    # The model may not declare something hostile; that call belongs to rules,
    # which an attacker cannot talk round.
    if disposition == "quarantine":
        return triage
    if disposition == triage.disposition and not reason:
        return triage

    tracing.emit(
        "triage_revised", cap=cap, msg_id=msg.id,
        was=triage.disposition, now=disposition,
    )
    return Triage(msg.id, disposition, reason or triage.reason, "model", triage.threat)
