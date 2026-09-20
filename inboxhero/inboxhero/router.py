"""The router: deciding what kind of problem each message is.

The lectures' point, applied to an inbox. Not all of this is an agent problem.

  HOSTILE   rules decide, alone. No model, because the text is adversarial.
  CHEAP     rules decide, alone. A Spotify receipt does not need reasoning,
            and paying a model to recognise one is the waste the brief warns
            about. On this mailbox that is 52 of 100 messages.
  WORKFLOW  a rule decides the disposition and a deterministic handler does
            the work: recording a preference, tracking a sent message.
  AGENTIC   the rest. Retrieval, drafting, scheduling against a stored
            preference, deciding what must go to a human.

Routing happens before anything expensive, and the route is recorded so the
manifest's claim about how many messages avoided a model call is checkable in
trace.jsonl rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import rules, tracing
from .agents import triage
from .mailstore import Message


class Route(str, Enum):
    HOSTILE = "hostile"
    CHEAP = "cheap"
    WORKFLOW = "workflow"
    AGENTIC = "agentic"


@dataclass
class Routed:
    msg: Message
    route: Route
    why: str

    @property
    def uses_model(self) -> bool:
        return self.route is Route.AGENTIC


def route(msg: Message, owner: str, owner_domain: str, *, cap: str | None = None) -> Routed:
    verdict = rules.classify_threat(msg, owner_domain)
    if verdict.hostile:
        decision = Routed(msg, Route.HOSTILE, f"{verdict.kind}: {verdict.attempted}")
    elif rules.cheap_disposition(msg):
        decision = Routed(msg, Route.CHEAP, "automated sender with no request in it")
    elif triage._STATES_PREFERENCE.search(f"{msg.subject}\n{msg.body}"):
        decision = Routed(msg, Route.WORKFLOW, "states a standing preference; record it")
    elif msg.sender.lower() == owner.lower() and msg.to.lower() != owner.lower():
        decision = Routed(msg, Route.WORKFLOW, "sent by the owner; track for a reply")
    else:
        decision = Routed(msg, Route.AGENTIC, "needs reading and judgement")

    tracing.emit("route", cap=cap, msg_id=msg.id, route=decision.route.value, why=decision.why)
    return decision
