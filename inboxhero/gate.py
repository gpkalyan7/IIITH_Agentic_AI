"""The approval gate.

Reversible and irreversible are properties of this mock mail store, not of
email in general:

  reversible    draft, label, archive, defer, flag, cc
                All of these are entries the owner can change or undo. An
                archived message is still in inbox.json; a draft is a file
                nobody has seen.

  irreversible  send, delete
                `send` writes to outbox/, which stands in for handing the
                message to a mail server. Once the owner's name is on it in
                someone else's inbox, no later action retracts it.
                `delete` is irreversible because this store has no trash. A
                deleted message is gone, so the decision cannot be revisited
                -- and a system that quietly deletes is a system whose
                mistakes are invisible, which is worse than the mistake.

Every irreversible action passes through `Gate.request`, which either prints
what it would do (dry-run) or asks a human (approval), and logs the proposal,
the answer, and the outcome either way. `tools.py` refuses to act on a request
this module did not approve, so there is no second path to the same effect.

"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from . import tracing

IRREVERSIBLE = ("send", "delete")
REVERSIBLE = ("draft", "label", "archive", "defer", "flag", "cc")


@dataclass
class Proposal:
    action: str
    msg_id: str
    summary: str
    why_gated: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_irreversible(self) -> bool:
        return self.action in IRREVERSIBLE


@dataclass
class Decision:
    proposal: Proposal
    approved: bool
    mode: str  # "dry-run" | "approval" | "auto"
    answer: str
    outcome: str = "pending"


class Gate:
    """Holds the only token tools.py will accept."""

    def __init__(self, *, dry_run: bool, interactive: bool = True, answers: list[str] | None = None):
        self.dry_run = dry_run
        self.interactive = interactive
        # Pre-recorded answers let a marker reproduce an approval session
        # without sitting at a prompt. Absent answers mean "deny".
        self._scripted = list(answers or [])
        self.decisions: list[Decision] = []
        self._approved_tokens: set[int] = set()

    # -- the gate --------------------------------------------------------

    def request(self, proposal: Proposal, *, cap: str | None = None) -> Decision:
        if not proposal.is_irreversible:
            decision = Decision(proposal, approved=True, mode="auto", answer="not gated")
            self._record(decision, cap)
            return decision

        if self.dry_run:
            decision = Decision(
                proposal, approved=False, mode="dry-run", answer="dry-run: not performed"
            )
            print(f"  [DRY-RUN] WOULD {proposal.action.upper()}: {proposal.summary}")
            print(f"            gated because: {proposal.why_gated}")
            for key, value in proposal.details.items():
                print(f"            {key}: {value}")
            self._record(decision, cap)
            return decision

        answer = self._ask(proposal)
        approved = answer.strip().lower() in ("y", "yes")
        decision = Decision(proposal, approved=approved, mode="approval", answer=answer)
        if approved:
            self._approved_tokens.add(id(proposal))
        self._record(decision, cap)
        return decision

    def _ask(self, proposal: Proposal) -> str:
        print()
        print(f"  APPROVAL NEEDED -- {proposal.action.upper()} ({proposal.msg_id})")
        print(f"    {proposal.summary}")
        print(f"    why you are being asked: {proposal.why_gated}")
        for key, value in proposal.details.items():
            print(f"    {key}: {value}")
        if self._scripted:
            answer = self._scripted.pop(0)
            print(f"    approve? [y/N]: {answer}   (scripted)")
            return answer
        if not self.interactive or not sys.stdin.isatty():
            print("    approve? [y/N]: n   (no terminal to ask at; denying)")
            return "n"
        try:
            return input("    approve? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("n   (interrupted; denying)")
            return "n"

    def _record(self, decision: Decision, cap: str | None) -> None:
        self.decisions.append(decision)
        if decision.proposal.is_irreversible:
            tracing.emit(
                "gate",
                cap=cap,
                action=decision.proposal.action,
                msg_id=decision.proposal.msg_id,
                proposed=decision.proposal.summary,
                why_gated=decision.proposal.why_gated,
                mode=decision.mode,
                human_answer=decision.answer,
                approved=decision.approved,
            )

    # -- the token tools.py checks ---------------------------------------

    def is_approved(self, proposal: Proposal) -> bool:
        return id(proposal) in self._approved_tokens

    def settle(self, decision: Decision, outcome: str) -> None:
        decision.outcome = outcome
        if decision.proposal.is_irreversible:
            tracing.emit(
                "gate_outcome",
                action=decision.proposal.action,
                msg_id=decision.proposal.msg_id,
                approved=decision.approved,
                outcome=outcome,
            )

    # -- reporting -------------------------------------------------------

    @property
    def pending(self) -> list[Decision]:
        """Irreversible things the system wanted to do and was not allowed to."""
        return [d for d in self.decisions if d.proposal.is_irreversible and not d.approved]

    @property
    def performed(self) -> list[Decision]:
        return [d for d in self.decisions if d.proposal.is_irreversible and d.approved]
