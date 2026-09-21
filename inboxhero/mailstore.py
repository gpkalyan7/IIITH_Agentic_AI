"""Read-only view over inbox.json.

Two things matter here beyond loading JSON:

1. Every retrieval of a message body emits a `read` trace event. Citation
   checking later asks "was this id read?" and the answer has to come from the
   store, not from the component that wants its citation to be accepted.

2. `known_addresses()` is the recipient allowlist used by the send tool. An
   address that has never appeared in the mailbox cannot be written to, which
   is what stops an exfiltration destination injected into a message body from
   being reachable even if a human mistakenly approved the send.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import tracing


@dataclass(frozen=True)
class Message:
    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    timestamp: str
    unread: bool
    body: str

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""

    def preview(self, width: int = 70) -> str:
        flat = " ".join(self.body.split())
        return flat[:width] + ("..." if len(flat) > width else "")


class MailStore:
    def __init__(self, path: Path, owner: str):
        self.path = Path(path)
        self.owner = owner.lower()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._messages: dict[str, Message] = {}
        self._order: list[str] = []
        for item in raw:
            msg = Message(
                id=item["id"],
                thread_id=item.get("thread_id", item["id"]),
                sender=item.get("from", ""),
                to=item.get("to", ""),
                subject=item.get("subject", ""),
                timestamp=item.get("timestamp", ""),
                unread=bool(item.get("unread", False)),
                body=item.get("body", ""),
            )
            self._messages[msg.id] = msg
            self._order.append(msg.id)
        self._order.sort(key=lambda mid: self._messages[mid].timestamp)
        tracing.emit("load", source=str(self.path), count=len(self._messages))

    # -- access ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._messages)

    def __contains__(self, msg_id: object) -> bool:
        return msg_id in self._messages

    def all(self) -> list[Message]:
        """Every message, oldest first. Does not count as reading a body."""
        return [self._messages[mid] for mid in self._order]

    def ids(self) -> list[str]:
        return list(self._order)

    def peek(self, msg_id: str) -> Message | None:
        """Header-level access with no read event. For indexing and counting."""
        return self._messages.get(msg_id)

    def get(self, msg_id: str, *, reason: str = "") -> Message:
        """Read a message. This is the only call that grounds a citation."""
        msg = self._messages.get(msg_id)
        if msg is None:
            raise KeyError(f"no such message: {msg_id}")
        tracing.emit("read", msg_id=msg_id, thread_id=msg.thread_id, reason=reason)
        return msg

    def thread(self, thread_id: str, *, reason: str = "thread-walk") -> list[Message]:
        """Every message in a thread, oldest first, all marked as read."""
        ids = [mid for mid in self._order if self._messages[mid].thread_id == thread_id]
        return [self.get(mid, reason=reason) for mid in ids]

    def thread_of(self, msg_id: str, *, reason: str = "thread-walk") -> list[Message]:
        msg = self._messages[msg_id]
        return self.thread(msg.thread_id, reason=reason)

    # -- indexes ---------------------------------------------------------

    def from_sender(self, needle: str) -> list[Message]:
        needle = needle.lower()
        return [m for m in self.all() if needle in m.sender.lower()]

    def sent_by_owner(self) -> list[Message]:
        """Messages the owner sent that are sitting in this store."""
        return [m for m in self.all() if m.sender.lower() == self.owner and m.to.lower() != self.owner]

    def inbound(self) -> list[Message]:
        return [m for m in self.all() if m.sender.lower() != self.owner]

    def known_addresses(self) -> set[str]:
        """Allowlist for outbound mail: every address the mailbox already knows."""
        known = {self.owner}
        for msg in self.all():
            for field in (msg.sender, msg.to):
                for addr in re.split(r"[,;\s]+", field or ""):
                    addr = addr.strip().lower()
                    if "@" in addr:
                        known.add(addr)
        return known

    def search(self, terms: list[str], *, exclude: set[str] | None = None, limit: int = 5) -> list[Message]:
        """Keyword fallback for cross-thread lookups. Scores on subject + body.

        Returns candidates without reading them; the caller reads what it uses,
        so the trace records what was actually consulted rather than what was
        merely offered.
        """
        exclude = exclude or set()
        terms = [t.lower() for t in terms if len(t) > 2]
        scored: list[tuple[int, Message]] = []
        for msg in self.all():
            if msg.id in exclude:
                continue
            haystack = f"{msg.subject} {msg.body}".lower()
            score = sum(haystack.count(t) for t in terms)
            score += sum(2 for t in terms if t in msg.subject.lower())
            if score:
                scored.append((score, msg))
        scored.sort(key=lambda pair: (-pair[0], pair[1].timestamp))
        return [msg for _, msg in scored[:limit]]
