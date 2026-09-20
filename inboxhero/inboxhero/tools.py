"""The only code that can affect anything outside this process.

Everything irreversible lives here, and every function here demands a Gate
decision that approved this exact proposal object. Passing a hand-made
Decision does not work: the gate hands out a token keyed to the proposal it
actually asked about.

On top of the gate, `send` enforces a recipient allowlist. Outbound mail may
only go to an address that already appears somewhere in inbox.json. This is
the belt to the gate's braces, and it is aimed squarely at Part 6: the
addresses the injected messages want mail sent to --
archive@mail-backup-service.info from m024, finance-sync@ext-audit.co from
m047 -- have never written to this mailbox, so they are unreachable. An
attacker would have to get their address into the mail store first, and then
still pass a human.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import tracing
from .gate import Decision, Gate, Proposal


class ActionRefused(RuntimeError):
    """An action was attempted without, or against, an approval."""


@dataclass
class Draft:
    msg_id: str
    to: str
    cc: list[str]
    subject: str
    body: str
    cites: list[str]
    rationale: str = ""

    def as_text(self) -> str:
        lines = [f"To: {self.to}"]
        if self.cc:
            lines.append(f"Cc: {', '.join(self.cc)}")
        lines += [
            f"Subject: {self.subject}",
            f"In-Reply-To: {self.msg_id}",
            f"X-Grounded-In: {', '.join(self.cites) if self.cites else '(none)'}",
            "",
            self.body,
        ]
        return "\n".join(lines)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class Toolbox:
    def __init__(self, *, outbox_dir: Path, gate: Gate, allowed_recipients: set[str]):
        self.outbox_dir = Path(outbox_dir)
        self.gate = gate
        self.allowed = {a.lower() for a in allowed_recipients}
        self.sent: list[Path] = []
        self.deleted: list[str] = []

    # -- reversible ------------------------------------------------------

    def draft(self, draft: Draft, *, cap: str | None = None) -> Draft:
        """Write nothing anywhere. A draft exists only in the run and the trace."""
        tracing.emit(
            "draft",
            cap=cap,
            msg_id=draft.msg_id,
            to=draft.to,
            cc=draft.cc,
            cites=draft.cites,
            chars=len(draft.body),
        )
        return draft

    # -- irreversible ----------------------------------------------------

    def send(self, draft: Draft, decision: Decision, *, cap: str | None = None) -> Path | None:
        """Write one file to outbox/, and nowhere else."""
        if decision.proposal.action != "send" or decision.proposal.msg_id != draft.msg_id:
            raise ActionRefused("approval does not correspond to this message")
        if not self.gate.is_approved(decision.proposal):
            self.gate.settle(decision, "not sent: no approval")
            return None

        recipients = [draft.to, *draft.cc]
        unknown = [r for r in recipients if r.lower() not in self.allowed]
        if unknown:
            # Refused after approval. A human said yes; the architecture says no.
            tracing.emit(
                "refusal",
                cap=cap,
                msg_id=draft.msg_id,
                refused="send",
                reason=f"recipient not present in the mail store: {', '.join(unknown)}",
            )
            self.gate.settle(decision, f"refused: unknown recipient {unknown}")
            raise ActionRefused(f"recipient not in mail store: {', '.join(unknown)}")

        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        name = _SAFE_NAME.sub("_", f"{draft.msg_id}_{stamp}")
        path = self.outbox_dir / f"{name}.eml"
        path.write_text(draft.as_text(), encoding="utf-8")
        (self.outbox_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "in_reply_to": draft.msg_id,
                    "to": draft.to,
                    "cc": draft.cc,
                    "subject": draft.subject,
                    "body": draft.body,
                    "grounded_in": draft.cites,
                    "approved_by_human": True,
                    "written_at": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.sent.append(path)
        tracing.emit("send", cap=cap, msg_id=draft.msg_id, to=draft.to, cc=draft.cc, file=str(path.name))
        self.gate.settle(decision, f"sent: {path.name}")
        return path

    def delete(self, msg_id: str, decision: Decision, *, cap: str | None = None) -> bool:
        """Implemented so it can be gated, and gated so it is never reached.

        Nothing in this system proposes a delete. It exists because Part 4 asks
        what is irreversible and why, and a claim about deletion is worth more
        when the code can actually do it and still does not.
        """
        if decision.proposal.action != "delete" or decision.proposal.msg_id != msg_id:
            raise ActionRefused("approval does not correspond to this message")
        if not self.gate.is_approved(decision.proposal):
            self.gate.settle(decision, "not deleted: no approval")
            return False
        self.deleted.append(msg_id)
        tracing.emit("delete", cap=cap, msg_id=msg_id)
        self.gate.settle(decision, "deleted")
        return True


def propose_send(draft: Draft, why: str) -> Proposal:
    preview = " ".join(draft.body.split())[:90]
    return Proposal(
        action="send",
        msg_id=draft.msg_id,
        summary=f"reply to {draft.to} re '{draft.subject}'",
        why_gated=why,
        details={
            "to": draft.to,
            "cc": ", ".join(draft.cc) or "(none)",
            "grounded in": ", ".join(draft.cites) or "(nothing cited)",
            "body": preview + ("..." if len(preview) == 90 else ""),
        },
    )
