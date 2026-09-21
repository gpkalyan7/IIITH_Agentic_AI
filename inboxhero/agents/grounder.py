"""Drafting a reply that is grounded in something the mailbox actually says.

The rule: a draft may only assert what appears in a message this run read. If
the evidence is not there, no draft is produced and the system says why. 

A model, when available, rewrites the draft so it reads like a person. It is
given the evidence and nothing else, and its output is checked afterwards:
every URL, figure and reference number in the rewritten text must already
appear in the evidence, or the rewrite is thrown away and the grounded
template stands. So the model can improve the prose and cannot introduce a
fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import envelope, retrieval, tracing
from ..mailstore import MailStore, Message
from ..memory import PreferenceStore
from ..tools import Draft


@dataclass
class DraftResult:
    msg_id: str
    draft: Draft | None
    grounded: bool
    method: str
    consulted: list[str] = field(default_factory=list)
    verified_cites: list[str] = field(default_factory=list)
    rejected_cites: list[str] = field(default_factory=list)
    refusal: str = ""
    sensitive: str = ""  # non-empty when the draft contains something that must not be sent lightly

    def to_dict(self) -> dict:
        return {
            "message": self.msg_id,
            "grounded": self.grounded,
            "retrieval": self.method,
            "consulted": self.consulted,
            "cited": self.verified_cites,
            "rejected_citations": self.rejected_cites,
            "refusal": self.refusal,
            "sensitive": self.sensitive,
            "draft": self.draft.as_text() if self.draft else None,
        }


# Things that must never be casually re-sent, even when correctly grounded.
_SECRET = re.compile(
    r"(?i)(amqp|postgres|mysql|redis|mongodb|https?)://[^\s]*:[^\s]*@|"
    r"\b(password|passwd|secret|api[_-]?key|token|credential)s?\b\s*[:=]|"
    r"://[a-z0-9_]+:[^\s@]+@"
)

_FACT = re.compile(
    r"[a-z][a-z0-9+.-]*://\S+|[$\u00a3\u20ac]\s?[\d,]+(?:\.\d{2})?|\b[A-Z]{2,}[-#]?\d{3,}\b|\b\d{1,2}:\d{2}\b"
)


def draft_reply(
    store: MailStore,
    msg: Message,
    prefs: PreferenceStore,
    client=None,
    *,
    cap: str | None = None,
) -> DraftResult:
    evidence = retrieval.gather(store, msg, cap=cap)

    if not evidence.grounded:
        refusal = (
            f"Nothing in the mailbox answers {msg.id}. Consulted "
            f"{', '.join(evidence.consulted) or 'no earlier messages'} via {evidence.method} "
            f"and found no statement of the fact being asked for. No draft written."
        )
        tracing.emit(
            "ungrounded", cap=cap, msg_id=msg.id,
            consulted=evidence.consulted, method=evidence.method,
        )
        return DraftResult(msg.id, None, False, evidence.method, evidence.consulted, refusal=refusal)

    verified, rejected = retrieval.verify_citations(store, evidence.cited_ids)
    if not verified:
        refusal = f"Every citation for {msg.id} failed verification: {', '.join(rejected)}. No draft written."
        return DraftResult(
            msg.id, None, False, evidence.method, evidence.consulted,
            rejected_cites=rejected, refusal=refusal,
        )

    body = _compose(msg, evidence, verified)
    cc = _apply_cc_preferences(msg, prefs)

    result = DraftResult(
        msg_id=msg.id,
        draft=Draft(
            msg_id=msg.id,
            to=msg.sender,
            cc=cc,
            subject=msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}",
            body=body,
            cites=verified,
            rationale=f"grounded in {', '.join(verified)} via {evidence.method}",
        ),
        grounded=True,
        method=evidence.method,
        consulted=evidence.consulted,
        verified_cites=verified,
        rejected_cites=rejected,
    )

    quoted = "\n".join(snippet for _, snippet in evidence.quotes)
    if _SECRET.search(quoted):
        result.sensitive = (
            "the grounded answer contains a live credential, so sending it re-transmits a secret"
        )

    if client is not None and client.available:
        _polish(result, msg, evidence, client, cap=cap)

    return result


def _compose(msg: Message, evidence: retrieval.Evidence, cites: list[str]) -> str:
    """The grounded template. Every sentence traces to a quote."""
    lines = ["Hi,", ""]
    lines.append("Answering from what is already in this thread:")
    lines.append("")
    seen = set()
    for msg_id, snippet in evidence.quotes:
        if snippet in seen:
            continue
        seen.add(snippet)
        lines.append(f"  - {snippet}   [{msg_id}]")
    lines += [
        "",
        f"That comes from {', '.join(cites)}; I have not added anything that is not in those messages.",
        "",
        "Best,",
        "Sam",
    ]
    return "\n".join(lines)


def _apply_cc_preferences(msg: Message, prefs: PreferenceStore) -> list[str]:
    """Honour standing 'always CC' preferences. Recorded once, applied every run."""
    cc: list[str] = []
    for pref in prefs.of_kind("always_cc"):
        # value is "address|matcher"; the matcher is tested against the sender.
        address, _, matcher = pref.value.partition("|")
        if matcher and matcher.lower() in f"{msg.sender} {msg.subject}".lower():
            if address not in cc:
                cc.append(address)
                tracing.emit(
                    "preference_applied", msg_id=msg.id, key=pref.key,
                    effect=f"cc {address}", because=pref.source_msg,
                )
    return cc


_SYSTEM = envelope.PREAMBLE + (
    "\n\nYou are rewriting a draft reply so it reads naturally. You may only use "
    "facts present in the EVIDENCE given to you. Do not add a greeting detail, a "
    "commitment, a date, a figure or a link that is not in the evidence. Reply with "
    'JSON only: {"body": "<the rewritten reply>"}'
)


def _polish(result: DraftResult, msg: Message, evidence: retrieval.Evidence, client, *, cap: str | None) -> None:
    facts = "\n".join(f"- [{mid}] {snippet}" for mid, snippet in evidence.quotes)
    prompt = (
        f"EVIDENCE (the only facts you may use):\n{facts}\n\n"
        f"The message being answered:\n{envelope.wrap(msg)}\n\n"
        f"Current draft:\n{result.draft.body}\n\nRewrite it."
    )
    response = client.complete_json(
        task="draft", system=_SYSTEM, prompt=prompt, schema={"body": str}, cap=cap
    )
    if not response or not response.get("body"):
        return

    candidate = response["body"].strip()
    allowed = " ".join(snippet for _, snippet in evidence.quotes)
    invented = [f for f in _FACT.findall(candidate) if f not in allowed]
    if invented:
        # The rewrite introduced something the evidence does not support.
        tracing.emit(
            "rewrite_rejected", cap=cap, msg_id=msg.id,
            reason="introduced facts absent from the evidence", invented=invented,
        )
        return

    result.draft.body = candidate + f"\n\n[grounded in {', '.join(result.verified_cites)}]"
    tracing.emit("rewrite_accepted", cap=cap, msg_id=msg.id)
