"""Finding, refusing and reporting hostile mail.

The detection itself is in rules.py, deliberately: a detector that asks the
model whether a message is an attack is asking the attacker's text to describe
itself. This module turns a rule verdict into the four things Part 6 requires
and makes sure none of them is silent.

  1. Not complying is the default, because there is no code path from a
     message body to an action. This module does not need to block anything;
     it records that there was something to block.
  2. A refusal event naming the message id and what was attempted.
  3. A line in the run summary. Reporting is not optional and not conditional:
     the summary is built from this list, so a hostile message that was found
     cannot fail to appear.
  4. The message stays. Nothing here archives or deletes; disposition is
     `quarantine`, which is visible, and the message remains in inbox.json.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import rules, tracing
from ..mailstore import MailStore, Message


@dataclass
class Refusal:
    msg_id: str
    sender: str
    subject: str
    kind: str  # "injection" | "phishing"
    attempted: str
    signals: list[str]
    destinations: list[str]
    did_instead: str

    def to_dict(self) -> dict:
        return {
            "message": self.msg_id,
            "from": self.sender,
            "subject": self.subject,
            "kind": self.kind,
            "attempted": self.attempted,
            "signals": self.signals,
            "destinations_requested": self.destinations,
            "what_the_system_did": self.did_instead,
        }

    def summary_line(self) -> str:
        return f"{self.msg_id} ({self.kind}): {self.attempted}"


_DID_INSTEAD = {
    "injection": (
        "refused; quarantined and left in place, not deleted; no outbox entry produced; "
        "no preference written"
    ),
    "phishing": (
        "refused; quarantined and left in place, not deleted; no reply drafted and no "
        "payment or credential action taken"
    ),
}


def scan(store: MailStore, owner_domain: str, *, cap: str | None = None) -> list[Refusal]:
    """Every hostile message in the mailbox, with what it wanted."""
    refusals: list[Refusal] = []
    for msg in store.all():
        verdict = rules.classify_threat(msg, owner_domain)
        if not verdict.hostile:
            continue
        refusals.append(_record(msg, verdict, cap=cap))
    return refusals


def inspect(msg: Message, owner_domain: str, *, cap: str | None = None) -> Refusal | None:
    verdict = rules.classify_threat(msg, owner_domain)
    if not verdict.hostile:
        return None
    return _record(msg, verdict, cap=cap)


def _record(msg: Message, verdict: rules.Verdict, *, cap: str | None) -> Refusal:
    destinations = rules.extract_destinations(msg.body) if verdict.kind == "injection" else []
    refusal = Refusal(
        msg_id=msg.id,
        sender=msg.sender,
        subject=msg.subject,
        kind=verdict.kind,
        attempted=verdict.attempted,
        signals=verdict.signal_names,
        destinations=destinations,
        did_instead=_DID_INSTEAD[verdict.kind],
    )
    tracing.emit(
        "refusal",
        cap=cap,
        msg_id=msg.id,
        kind=verdict.kind,
        refused=verdict.attempted,
        signals=verdict.signal_names,
        destinations=destinations,
        action_taken="none",
        left_in_place=True,
        deleted=False,
    )
    return refusal
