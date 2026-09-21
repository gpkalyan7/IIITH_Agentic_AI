"""The capabilities that are extra other than the brief's (Part 8).

  X1  sender lookup          tier A -- one lookup, one output, no model
  X2  thread to open question tier B -- reads a whole thread, returns the ask
  X3  morning digest          tier B -- needs you / can wait / handled for you
  X4  follow-up tracking      tier B -- sent mail nobody answered
  X5  decision provenance     tier C -- "why did you do that?", answered from
                                        the trace rather than from a narrative
                                        the system makes up after the fact
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import envelope, tracing
from ..mailstore import MailStore, Message


# -- X1: sender lookup (tier A) ---------------------------------------------


def sender_lookup(store: MailStore, needle: str, *, unread_only: bool = True, cap: str = "X1") -> dict:
    matches = [m for m in store.from_sender(needle) if m.unread or not unread_only]
    tracing.emit("lookup", cap=cap, query=needle, unread_only=unread_only, found=len(matches))
    return {
        "query": needle,
        "unread_only": unread_only,
        "count": len(matches),
        "messages": [
            {"id": m.id, "from": m.sender, "subject": m.subject, "date": m.timestamp, "unread": m.unread}
            for m in matches
        ],
    }


# -- X2: thread down to the open question (tier B) --------------------------

_ASK = re.compile(
    r"(?i)((?:[^.!?]*\b(?:can|could|would|please|needs?|by the \d{1,2}|approve|confirm)\b[^.!?]*[.?!]))"
)
_DIRECTED = re.compile(r"(?i)\bneeds (\w+) specifically|\b(\w+), can you\b|\bone thing that needs\b")


def thread_open_question(store: MailStore, thread_id: str, owner: str, client=None, *, cap: str = "X2") -> dict:
    messages = store.thread(thread_id, reason=f"summarising {thread_id}")
    if not messages:
        return {"thread": thread_id, "error": "no such thread"}

    first_name = owner.split("@")[0].lower()
    scored: list[tuple[int, str, str]] = []
    for msg in messages:
        if msg.sender.lower() == owner.lower():
            continue
        directed = 1 if _DIRECTED.search(msg.body) else 0
        for sentence in _ASK.findall(msg.body):
            sentence = " ".join(sentence.split())
            scored.append((_score_ask(sentence, first_name) + directed, msg.id, sentence))

    # A thread contains several sentences that look like asks. The one still
    # open is the one actually addressed to the owner, phrased as a request,
    # and carrying a date: in t-launch that is "Sam, can you approve the final
    # pricing copy by the 12th?" rather than the sentence introducing it.
    scored.sort(key=lambda item: (-item[0], item[1]))
    open_question = (scored[0][1], scored[0][2]) if scored else (None, "")
    summary = _summarise(messages, client, cap=cap)

    tracing.emit(
        "thread_summary", cap=cap, thread_id=thread_id, messages=len(messages),
        open_question_from=open_question[0],
    )
    return {
        "thread": thread_id,
        "message_count": len(messages),
        "participants": sorted({m.sender for m in messages}),
        "span": f"{messages[0].timestamp} to {messages[-1].timestamp}",
        "summary": summary,
        "open_question": open_question[1],
        "open_question_from": open_question[0],
        "all_messages": [m.id for m in messages],
    }


def _score_ask(sentence: str, first_name: str) -> int:
    lowered = sentence.lower()
    score = 0
    if sentence.rstrip().endswith("?"):
        score += 3
    if re.search(r"\bby (?:the )?\d{1,2}(?:st|nd|rd|th)?\b|\bby \w+day\b", lowered):
        score += 2
    if re.search(r"\b(can|could|would) you\b|\bplease\b", lowered):
        score += 2
    if first_name in lowered:
        score += 2
    return score


def _summarise(messages: list[Message], client, *, cap: str) -> str:
    baseline = "; ".join(f"{m.sender.split('@')[0]}: {m.preview(60)}" for m in messages[:6])
    if client is None or not client.available:
        return baseline
    response = client.complete_json(
        task="thread_summary",
        system=envelope.PREAMBLE + '\n\nReply with JSON only: {"summary": "<two sentences>"}',
        prompt=envelope.wrap_many(messages, max_body=500),
        schema={"summary": str},
        cap=cap,
    )
    return (response or {}).get("summary") or baseline


# -- X3: morning digest (tier B) --------------------------------------------


def digest(result, store: MailStore, *, cap: str = "X3") -> dict:
    needs_you, can_wait, handled = [], [], {}
    for decision in result.decisions:
        msg = store.peek(decision.msg_id)
        entry = {
            "id": decision.msg_id,
            "from": msg.sender if msg else "",
            "subject": msg.subject if msg else "",
            "why": decision.reason,
        }
        if decision.disposition in ("escalate", "ask", "quarantine"):
            needs_you.append(entry)
        elif decision.disposition in ("reply", "defer", "delegate"):
            can_wait.append(entry)
        else:
            handled[msg.sender_domain if msg else "?"] = handled.get(
                msg.sender_domain if msg else "?", 0
            ) + 1

    tracing.emit("digest", cap=cap, needs_you=len(needs_you), can_wait=len(can_wait))
    return {
        "needs_you": needs_you,
        "can_wait": can_wait,
        # Auto-archived mail is reported by count, not individually. Listing
        # 67 receipts is how a digest stops being read.
        "handled_for_you": {
            "total": sum(handled.values()),
            "by_sender_domain": dict(sorted(handled.items(), key=lambda kv: -kv[1])[:8]),
        },
        "conflicts": [c.to_dict() for c in result.conflicts],
        "next_deadlines": [c.to_dict() for c in result.calendar[:5]],
    }


# -- X4: follow-up tracking (tier B) ----------------------------------------


@dataclass
class FollowUp:
    msg_id: str
    to: str
    subject: str
    sent: str
    days_waiting: int
    draft: str

    def to_dict(self) -> dict:
        return {
            "message_id": self.msg_id,
            "to": self.to,
            "subject": self.subject,
            "sent": self.sent,
            "days_waiting": self.days_waiting,
            "draft": self.draft,
        }


def follow_ups(store: MailStore, owner: str, *, min_days: int = 3, now: datetime | None = None, cap: str = "X4") -> dict:
    now = now or max(m.when for m in store.all())
    pending: list[FollowUp] = []
    answered: list[str] = []

    for msg in store.sent_by_owner():
        thread = [m for m in store.all() if m.thread_id == msg.thread_id]
        replies = [
            m for m in thread if m.timestamp > msg.timestamp and m.sender.lower() != owner.lower()
        ]
        if replies:
            answered.append(msg.id)
            continue
        waiting = (now - msg.when).days
        if waiting < min_days:
            continue
        store.get(msg.id, reason="follow-up check")
        pending.append(
            FollowUp(
                msg_id=msg.id,
                to=msg.to,
                subject=msg.subject,
                sent=msg.timestamp,
                days_waiting=waiting,
                draft=(
                    f"Hi,\n\nFollowing up on '{msg.subject.replace('Re: ', '')}' from "
                    f"{msg.when:%d %b} -- {waiting} days without a reply. Any update?\n\nBest,\nSam"
                ),
            )
        )

    tracing.emit("follow_up", cap=cap, pending=[f.msg_id for f in pending], answered=answered)
    return {
        "threshold_days": min_days,
        "as_of": now.isoformat(timespec="seconds"),
        "unanswered": [f.to_dict() for f in pending],
        "answered_in_thread_so_excluded": answered,
    }


# -- X5: decision provenance (tier C) ---------------------------------------

_EXPLAIN = {
    "route": "routed as {route}, because {why}",
    "read": "read {msg_id} ({reason})",
    "decision": "assigned '{disposition}' by {decided_by}, because {reason}",
    "retrieval": "retrieved evidence via {method}; consulted {consulted}, cited {cited}",
    "draft": "drafted a reply to {to}, grounded in {cites}",
    "ungrounded": "declined to draft: nothing in {consulted} answered it",
    "gate": "proposed {action}; gate mode {mode}; human said '{human_answer}' -> approved={approved}",
    "gate_outcome": "outcome: {outcome}",
    "send": "wrote {file} to outbox/",
    "refusal": "REFUSED: {refused}",
    "preference_stored": "stored preference {key} = {value}",
    "preference_applied": "applied preference {key}: {effect} (stated in {because})",
    "preference_rejected": "rejected a proposed preference: {reason}",
    "commitment": "extracted commitment '{title}' for {when}, citing {cites}",
    "rewrite_rejected": "threw away the model's rewrite: {reason}",
    "llm_call": "called the model for '{task}' (cached={cached})",
    "triage_revised": "model changed the disposition from {was} to {now}",
}


@dataclass
class Explanation:
    msg_id: str
    steps: list[str] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"message": self.msg_id, "steps": self.steps, "events": self.raw}


def explain(trace_path, msg_id: str, *, cap: str = "X5") -> Explanation:
    """Reconstruct what happened to one message, from the log, not from memory.

    This deliberately reads trace.jsonl rather than any in-process state. An
    explanation that comes from the same run that made the decision is a story;
    one that comes from the log is evidence, and it is still available
    tomorrow when somebody asks why a message went out.
    """
    events = tracing.load_trace(trace_path)
    explanation = Explanation(msg_id)
    for event in events:
        if event.get("msg_id") != msg_id:
            # Commitments and conflicts reference ids in a list instead.
            cites = event.get("cites") or event.get("from_msgs") or []
            if msg_id not in cites:
                continue
        explanation.raw.append(event)
        template = _EXPLAIN.get(event.get("event", ""))
        if not template:
            continue
        try:
            explanation.steps.append(f"[{event.get('ts', '')}] " + template.format(**event))
        except (KeyError, IndexError):
            explanation.steps.append(f"[{event.get('ts', '')}] {event.get('event')}")
    return explanation
