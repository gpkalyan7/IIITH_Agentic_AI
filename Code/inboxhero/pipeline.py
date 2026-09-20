"""The crew: what runs, in what order, and what comes out.

A framework would call this the orchestrator. It is a function, because the
work is a linear pass with one branch, and the branch is the router.

  load -> route -> dispose -> record preferences -> retrieve and draft
       -> check schedules against preferences -> gate anything irreversible
       -> extract commitments -> report

The ordering carries one real constraint: preferences are recorded before
drafting, so a preference stated in this mailbox affects drafts in the same
run as well as in later ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import tracing
from .agents import commitments, grounder, scheduler, sentinel, triage
from .agents.sentinel import Refusal
from .gate import Gate, Proposal
from .llm import LLMClient
from .mailstore import MailStore, Message
from .memory import PreferenceRejected, PreferenceStore
from .router import Route, Routed, route
from .tools import Draft, Toolbox, propose_send


@dataclass
class PendingAction:
    msg_id: str
    action: str
    summary: str
    why_human: str

    def to_dict(self) -> dict:
        return {
            "message": self.msg_id,
            "proposed_action": self.action,
            "what": self.summary,
            "why_it_needs_a_human": self.why_human,
        }


@dataclass
class RunResult:
    decisions: list[triage.Triage] = field(default_factory=list)
    routes: dict[str, Routed] = field(default_factory=dict)
    refusals: list[Refusal] = field(default_factory=list)
    drafts: list[grounder.DraftResult] = field(default_factory=list)
    schedule_checks: list[scheduler.SchedulingCheck] = field(default_factory=list)
    calendar: list[commitments.Commitment] = field(default_factory=list)
    conflicts: list[commitments.Conflict] = field(default_factory=list)
    pending: list[PendingAction] = field(default_factory=list)
    preferences_recorded: list[str] = field(default_factory=list)
    preferences_rejected: list[str] = field(default_factory=list)
    rule_handled: int = 0
    model_eligible: int = 0
    llm_status: str = ""

    @property
    def undecided(self) -> list[str]:
        return [d.msg_id for d in self.decisions if d.disposition not in triage.DISPOSITIONS]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for decision in self.decisions:
            out[decision.disposition] = out.get(decision.disposition, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# Preferences the system knows how to extract, as (pattern, kind, key, value).
_PREFERENCE_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"(?i)do not take meetings before (\d{1,2})(?::(\d{2}))?\s*(am|pm)"),
        "scheduling_window",
        "earliest_meeting",
        "",
    ),
    (
        re.compile(r"(?i)(?:cc'?d|copy|loop me in).{0,60}?(?:lawyers|counsel) at ([A-Za-z&' ]+)"),
        "always_cc",
        "cc_on_legal",
        "",
    ),
]


def run(
    *,
    store: MailStore,
    prefs: PreferenceStore,
    gate: Gate,
    toolbox: Toolbox,
    client: LLMClient,
    owner: str,
    owner_domain: str,
    cap: str | None = None,
    use_model: bool = True,
    send_approved: bool = True,
) -> RunResult:
    result = RunResult(llm_status=client.status())

    # 1. Route everything before spending anything.
    routed = [route(msg, owner, owner_domain, cap=cap) for msg in store.all()]
    result.routes = {r.msg.id: r for r in routed}
    result.rule_handled = sum(1 for r in routed if not r.uses_model)
    result.model_eligible = sum(1 for r in routed if r.uses_model)

    # 2. Record preferences first, so this run honours them too.
    for decision in routed:
        if decision.route is Route.WORKFLOW:
            _record_preferences(decision.msg, prefs, result)

    # 3. A disposition for every message, without exception.
    for decision in routed:
        assigned = triage.baseline(decision.msg, owner, owner_domain)
        if use_model and decision.uses_model:
            assigned = triage.refine(assigned, decision.msg, client, owner, cap=cap)
        result.decisions.append(assigned)
        tracing.emit(
            "decision", cap=cap, msg_id=assigned.msg_id,
            disposition=assigned.disposition, reason=assigned.reason,
            decided_by=assigned.decided_by, route=decision.route.value,
        )

    # 4. Hostile mail: refuse, flag, report. Never delete.
    result.refusals = sentinel.scan(store, owner_domain, cap=cap)

    # 5. Commitments, from everything that is neither noise nor hostile.
    considered = [
        store.get(r.msg.id, reason="commitment scan")
        for r in routed
        if r.route in (Route.AGENTIC, Route.WORKFLOW)
    ]
    result.calendar, result.conflicts = commitments.extract(store, considered, cap=cap)

    # 6. Draft where a reply is owed, and check anything that proposes a time.
    by_id = {d.msg_id: d for d in result.decisions}
    for decision in routed:
        if decision.route is not Route.AGENTIC:
            continue
        msg = decision.msg
        disposition = by_id[msg.id].disposition

        if scheduler.is_scheduling_request(msg):
            check = scheduler.check(msg, prefs, result.calendar, cap=cap)
            result.schedule_checks.append(check)
            if check.needs_pushback:
                _propose_pushback(msg, check, prefs, gate, toolbox, result, cap=cap, send=send_approved)
                continue

        if disposition == "reply":
            outcome = grounder.draft_reply(store, msg, prefs, client if use_model else None, cap=cap)
            result.drafts.append(outcome)
            if outcome.draft is not None:
                toolbox.draft(outcome.draft, cap=cap)
                _gate_send(msg, outcome, gate, toolbox, result, cap=cap, send=send_approved)

        elif disposition in ("escalate", "ask"):
            result.pending.append(
                PendingAction(
                    msg.id,
                    "await the owner",
                    f"{msg.subject} -- from {msg.sender}",
                    by_id[msg.id].reason,
                )
            )

    return result


# -- helpers -----------------------------------------------------------------


def _record_preferences(msg: Message, prefs: PreferenceStore, result: RunResult) -> None:
    text = f"{msg.subject}\n{msg.body}"

    match = _PREFERENCE_RULES[0][0].search(text)
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "pm":
            hour += 12
        _try_store(
            prefs, result,
            key="earliest_meeting", kind="scheduling_window",
            value=f"{hour:02d}:{minute:02d}", source_msg=msg.id, stated_by=msg.sender,
            description=f"no meetings before {hour:02d}:{minute:02d}",
        )

    match = _PREFERENCE_RULES[1][0].search(text)
    if match:
        firm = match.group(1).strip().lower().replace(" & ", "").replace(" ", "")
        _try_store(
            prefs, result,
            key="cc_on_legal", kind="always_cc",
            # "address|matcher": copy this address when the matcher hits.
            value=f"{msg.sender}|{firm[:8]}", source_msg=msg.id, stated_by=msg.sender,
            description=f"always CC {msg.sender} on mail from {match.group(1).strip()}",
        )


def _try_store(prefs: PreferenceStore, result: RunResult, **kwargs) -> None:
    try:
        pref = prefs.remember(**kwargs)
        result.preferences_recorded.append(f"{pref.key}: {pref.description} (from {pref.source_msg})")
    except PreferenceRejected as exc:
        result.preferences_rejected.append(f"{kwargs.get('source_msg')}: {exc}")
        tracing.emit(
            "preference_rejected", msg_id=kwargs.get("source_msg"), reason=str(exc)
        )


def _gate_send(
    msg: Message, outcome: grounder.DraftResult, gate: Gate, toolbox: Toolbox,
    result: RunResult, *, cap: str | None, send: bool,
) -> None:
    why = _why_gated(msg, outcome)
    proposal = propose_send(outcome.draft, why)
    decision = gate.request(proposal, cap=cap)
    if decision.approved and send:
        try:
            toolbox.send(outcome.draft, decision, cap=cap)
        except Exception as exc:  # recipient allowlist, mostly
            tracing.emit("send_failed", cap=cap, msg_id=msg.id, reason=str(exc))
    if not decision.approved:
        result.pending.append(PendingAction(msg.id, "send", proposal.summary, why))


def _why_gated(msg: Message, outcome: grounder.DraftResult) -> str:
    if outcome.sensitive:
        return outcome.sensitive
    return "sending cannot be undone once the message carries the owner's name"


def _propose_pushback(
    msg: Message, check: scheduler.SchedulingCheck, prefs: PreferenceStore,
    gate: Gate, toolbox: Toolbox, result: RunResult, *, cap: str | None, send: bool,
) -> None:
    draft = Draft(
        msg_id=msg.id,
        to=msg.sender,
        cc=grounder._apply_cc_preferences(msg, prefs),
        subject=f"Re: {msg.subject}",
        body=scheduler.compose_pushback(msg, check),
        cites=check.cites,
        rationale=check.violates or "slot already taken",
    )
    toolbox.draft(draft, cap=cap)
    why = (
        f"declines a request from {msg.sender} on the owner's behalf; "
        f"{check.violates or 'the slot clashes with an existing commitment'}"
    )
    proposal = propose_send(draft, why)
    decision = gate.request(proposal, cap=cap)
    if decision.approved and send:
        try:
            toolbox.send(draft, decision, cap=cap)
        except Exception as exc:
            tracing.emit("send_failed", cap=cap, msg_id=msg.id, reason=str(exc))
    if not decision.approved:
        result.pending.append(PendingAction(msg.id, "send", proposal.summary, why))
