"""Deterministic detectors.

Two jobs, both done before any model is consulted.

*Cheap mail.* Receipts, newsletters and service notifications are recognised by
sender shape and subject pattern. Recognising a Spotify receipt does not need a
language model, and paying for one to do it is the mistake the brief warns
about. These messages get their disposition here and never enter a prompt.

*Hostile mail.* Injections and phishing are caught by rule too, deliberately.
A detector that depends on the model is a detector an attacker can talk their
way past, because the attacker controls the text the model is reading. Rules
are not smarter than the model, but they cannot be persuaded.

The model's view of a hostile message is advisory only: it can raise a
suspicion the rules missed, but it cannot clear one the rules raised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .mailstore import Message

# --------------------------------------------------------------------------
# Cheap mail
# --------------------------------------------------------------------------

_AUTOMATED_LOCALPART = re.compile(
    r"^(no[-_.]?reply|noreply|do[-_.]?not[-_.]?reply|receipts?|invoice\S*|billing|"
    r"statements?|notifications?|notify|alerts?|newsletter|digest|updates?|info|"
    r"news|mailer|orders?|ship[-_.]?confirm|checkin|feedback|insights|hello|support|"
    r"calendar[-_.]?notification|security)"
    r"([-+.].*)?$",
    re.IGNORECASE,
)

_NOISE_SUBJECT = re.compile(
    r"(?i)\b(your (receipt|invoice|bill|statement|order|subscription|payout|ride|trip|flight)"
    r"|receipt from|invoice (paid|from)|order has shipped|is delivered|bill is ready"
    r"|weekly (activity|analytics|digest|uptime)|monthly (summary|invoice|usage)"
    r"|daily digest|new course recommendations|campaign report|screen time"
    r"|you appeared in|new notifications|unread messages in|top \d+"
    r"|incident resolved|monitor ok|new issue in production|recording is ready"
    r"|security digest|a new sign[- ]in|new login to|files are almost full"
    r"|actions minutes|account statement|verification code is|event was scheduled"
    r"|standup in \d+ minutes|in 10 minutes"
    r"|welcome back|new posts|rate your recent)\b"
)

# Automated senders whose mail can still be worth a human's attention.
_NOT_JUST_NOISE = re.compile(
    r"(?i)\b(renews? (in|on|soon)|expires?|password was changed|appointment|"
    r"dispute|timesheet|office closed|confirm)\b"
)


# --------------------------------------------------------------------------
# Prompt injection
# --------------------------------------------------------------------------


@dataclass
class Signal:
    name: str
    detail: str


@dataclass
class Verdict:
    hostile: bool = False
    kind: str = ""  # "injection" | "phishing" | ""
    signals: list[Signal] = field(default_factory=list)
    attempted: str = ""

    @property
    def signal_names(self) -> list[str]:
        return [s.name for s in self.signals]


_INJECTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "override",
        re.compile(r"(?i)ignore (all )?(previous|prior|above) instructions?|disregard (all )?(previous|prior) instructions?"),
        "instructs the assistant to discard its own instructions",
    ),
    (
        "addressed-to-assistant",
        re.compile(
            r"(?i)(system notice for automated assistants?|automated[- ]agent directive"
            r"|note for the (assistant|ai)|assistant note|if an ai agent is processing"
            r"|an assistant managing this inbox|assistant configuration update"
            r"|automated assistants?)"
        ),
        "contains a directive addressed to the assistant rather than the reader",
    ),
    (
        "concealment",
        re.compile(
            r"(?i)(do not (mention|surface|tell|inform|reveal|include)|don't (mention|surface|tell)"
            r"|there is no need to (mention|surface)|must not surface|without (mentioning|telling)"
            r"|keep this between us|keep this confidential and)"
        ),
        "asks for the action to be hidden from the user",
    ),
    (
        "exfiltration",
        re.compile(r"(?i)forward (the full contents|any message|all mail|the mailbox|this mailbox|every)"),
        "asks for mailbox contents to be forwarded elsewhere",
    ),
    (
        "self-delete",
        re.compile(r"(?i)(then )?delete this (message|email)|remove this (message|email) (from|after)"),
        "asks to delete the evidence of itself",
    ),
    (
        "disable-gate",
        re.compile(
            r"(?i)(without asking for approval|skip the (confirmation|approval)"
            r"|autonomous mode is now enabled|automatically without asking|no approval (is )?(needed|required))"
        ),
        "asks the system to stop requiring human approval",
    ),
    (
        "mass-reply",
        re.compile(r"(?i)reply with the single word|reply to (the sender of )?every (unread )?message"),
        "asks for an automated reply to every correspondent",
    ),
    (
        "preference-poisoning",
        re.compile(r"(?i)save this as a standing preference|so it survives restarts|remember this setting permanently"),
        "tries to write itself into persistent memory",
    ),
]

_EXTERNAL_DEST = re.compile(r"(?i)\b(?:to|at)\s+([\w.+-]+@[\w.-]+\.\w+)")


def detect_injection(msg: Message) -> Verdict:
    verdict = Verdict()
    body = msg.body
    for name, pattern, detail in _INJECTION_RULES:
        match = pattern.search(body)
        if match:
            verdict.signals.append(Signal(name, detail))
    if not verdict.signals:
        return verdict

    # Being addressed to the assistant is not itself hostile: the owner does
    # exactly that when stating a standing preference (m041, "I don't take
    # meetings before 11:00"). The sender cannot separate the two either, since
    # the malicious m039 also arrives as sam@paperjet.io. What separates them is
    # what is asked for. So `addressed-to-assistant` only counts alongside a
    # second signal, while asking to conceal, exfiltrate, act en masse, or
    # remove a control is on its own enough.
    strong = {"override", "exfiltration", "disable-gate", "mass-reply"}
    if not (set(verdict.signal_names) & strong) and len(verdict.signals) < 2:
        verdict.signals = []
        return verdict

    verdict.hostile = True
    verdict.kind = "injection"
    verdict.attempted = _describe_injection(msg, verdict)
    return verdict


def _describe_injection(msg: Message, verdict: Verdict) -> str:
    names = set(verdict.signal_names)
    parts: list[str] = []
    if "exfiltration" in names:
        dests = extract_destinations(msg.body)
        where = f" to {', '.join(dests)}" if dests else " to an external address"
        parts.append(f"forward mailbox contents{where}")
    if "mass-reply" in names:
        parts.append("send an automated reply to every unread correspondent")
    if "disable-gate" in names:
        parts.append("disable the human-approval gate")
    if "preference-poisoning" in names:
        parts.append("write itself into the persistent preference store")
    if "self-delete" in names:
        parts.append("delete itself afterwards")
    if "concealment" in names:
        parts.append("hide the action from the user")
    if not parts:
        parts.append("issue instructions to the assistant reading the mailbox")
    return "; ".join(parts)


def extract_destinations(body: str) -> list[str]:
    """Addresses a message asks mail to be sent to."""
    return sorted({addr.lower() for addr in _EXTERNAL_DEST.findall(body)})


# --------------------------------------------------------------------------
# Phishing
# --------------------------------------------------------------------------

_PHISH_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "payment-redirect",
        re.compile(
            r"(?i)(banking partner has changed|updated remittance|new (bank|account) details"
            r"|disregard the account on file|remit .{0,40}to the new account|change of bank)"
        ),
        "asks for payment to be redirected to a new account",
    ),
    (
        "urgent-wire",
        re.compile(r"(?i)(wire \$?[\d,]+|transfer \$?[\d,]+ (now|today)|secure the .{0,30}deposit (today|now))"),
        "requests an urgent funds transfer",
    ),
    (
        "credential-harvest",
        re.compile(
            r"(?i)(password (will )?expires? in|re[- ]?verify your credentials"
            r"|verify your (account|password|credentials) (immediately|today|now)"
            r"|accounts that are not verified)"
        ),
        "pressures the reader into entering credentials",
    ),
    (
        "bypass-controls",
        re.compile(r"(?i)(don'?t loop in finance|without (involving|telling) finance|keep this between us|don'?t tell anyone)"),
        "asks the reader to bypass normal controls",
    ),
    (
        "manufactured-urgency",
        re.compile(
            r"(?i)(before end of day to avoid|within 2 hours|in 2 hours|avoid a service interruption"
            r"|will be suspended|expires in 2 hours|same-day)"
        ),
        "manufactures a deadline to suppress checking",
    ),
]

_URL = re.compile(r"https?://([\w.-]+)")


def lookalike_domain(domain: str, owner_domain: str) -> bool:
    """True when a domain is dressed up to pass for the owner's own.

    Deliberately narrow. `paperjet-board.org` and `paperjet-monitoring.io` are
    real correspondents in this mailbox, so brand-in-domain alone proves
    nothing; it only counts as a signal alongside hostile intent, which is why
    `classify_threat` requires both.
    """
    domain = (domain or "").lower()
    owner_domain = owner_domain.lower()
    if not domain or domain == owner_domain:
        return False
    brand = owner_domain.split(".")[0]
    if not domain.startswith(brand):
        return False
    # Same brand, different domain: paperjet.co, paperjet-helpdesk.com, ...
    return True


def detect_phishing(msg: Message, owner_domain: str) -> Verdict:
    verdict = Verdict()
    for name, pattern, detail in _PHISH_RULES:
        if pattern.search(msg.body) or pattern.search(msg.subject):
            verdict.signals.append(Signal(name, detail))

    impersonating = lookalike_domain(msg.sender_domain, owner_domain)
    if impersonating:
        verdict.signals.append(
            Signal("lookalike-sender", f"sender domain {msg.sender_domain} imitates {owner_domain}")
        )

    for host in _URL.findall(msg.body):
        if lookalike_domain(host, owner_domain):
            verdict.signals.append(Signal("lookalike-link", f"link host {host} imitates {owner_domain}"))

    strong = {"payment-redirect", "urgent-wire", "credential-harvest"}
    names = set(verdict.signal_names)
    if not (names & strong):
        verdict.signals = []
        return verdict

    verdict.hostile = True
    verdict.kind = "phishing"
    verdict.attempted = _describe_phish(names)
    return verdict


def _describe_phish(names: set[str]) -> str:
    parts = []
    if "payment-redirect" in names:
        parts.append("redirect an outstanding payment to an attacker-controlled account")
    if "urgent-wire" in names:
        parts.append("obtain an urgent funds transfer")
    if "credential-harvest" in names:
        parts.append("collect mailbox credentials via a look-alike login page")
    if "lookalike-sender" in names:
        parts.append("impersonate an internal colleague")
    return "; ".join(parts)


def classify_threat(msg: Message, owner_domain: str) -> Verdict:
    """Injection first: it is the one that tries to act through the system."""
    injection = detect_injection(msg)
    if injection.hostile:
        return injection
    return detect_phishing(msg, owner_domain)


# --------------------------------------------------------------------------
# Cheap-mail classification
# --------------------------------------------------------------------------


def is_automated_sender(msg: Message) -> bool:
    local = msg.sender.split("@", 1)[0] if "@" in msg.sender else msg.sender
    return bool(_AUTOMATED_LOCALPART.match(local))


def cheap_disposition(msg: Message) -> tuple[str, str] | None:
    """Disposition for mail that needs no reasoning, or None to escalate to the model."""
    if not is_automated_sender(msg):
        return None
    if _NOT_JUST_NOISE.search(msg.subject) or _NOT_JUST_NOISE.search(msg.body):
        return None
    if _NOISE_SUBJECT.search(msg.subject):
        return "archive", f"automated notification from {msg.sender}; no action requested"
    # Automated sender, unrecognised subject: still cheap, but say so honestly.
    return "archive", f"no-reply sender {msg.sender}; nothing to respond to"
