"""Finding the earlier message that answers the current one.

**Method: thread-walk first, keyword search as fallback.** An inbox already
carries a correct, human-maintained structure in `thread_id`. Walking it is
exact, free, and explains itself -- "I used m003 because it is the message
before yours in this thread" is a reason a person can check. Embeddings would
add a similarity score, an index to build, and a dependency, in exchange for
worse precision on the case that actually matters here: m008 asks for a URL
that appears in exactly one message of its own thread. Keyword search covers
the cross-thread case, where the answer is not in the thread you are standing
in.

**Citations are checked, not trusted.** `verify_citations` accepts a cited id
only if the id exists in the store and the run's trace shows it was read. The
read events come from MailStore.get, so a component cannot make its own
citation pass by asserting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import tracing
from .mailstore import MailStore, Message

_STOPWORDS = {
    "the", "and", "for", "you", "your", "our", "can", "was", "are", "with", "that",
    "this", "have", "has", "from", "not", "but", "any", "all", "get", "got", "did",
    "ever", "chance", "sort", "out", "about", "after", "before", "need", "needs",
    "please", "thanks", "hey", "hi", "just", "one", "more", "back", "when", "what",
    "will", "would", "could", "should", "there", "their", "them", "they", "been",
    "re", "fwd", "sec", "kind", "thing", "some", "much", "very", "let", "know",
}


@dataclass
class Evidence:
    """What a draft is allowed to say, and where each part came from."""

    quotes: list[tuple[str, str]] = field(default_factory=list)  # (msg_id, snippet)
    consulted: list[str] = field(default_factory=list)
    method: str = "thread-walk"

    @property
    def cited_ids(self) -> list[str]:
        seen, out = set(), []
        for msg_id, _ in self.quotes:
            if msg_id not in seen:
                seen.add(msg_id)
                out.append(msg_id)
        return out

    @property
    def grounded(self) -> bool:
        return bool(self.quotes)


def keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


# Things a request can be asking for, and how to recognise the answer.
_ANSWER_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("url", re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)),
    ("date", re.compile(r"\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.IGNORECASE)),
    ("time", re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE)),
    ("money", re.compile(r"[$£€]\s?[\d,]+(?:\.\d{2})?")),
    ("case-ref", re.compile(r"\b[A-Z]{2,}[-#]?\d{3,}\b")),
]


def _wants(text: str) -> list[str]:
    """What kind of fact is being asked for."""
    lowered = text.lower()
    wanted = []
    if re.search(r"\b(url|link|address|creds?|credentials?|password|endpoint|resend)\b", lowered):
        wanted.append("url")
    if re.search(r"\b(when|date|day|deadline|schedule|time)\b", lowered):
        wanted += ["date", "time"]
    if re.search(r"\b(how much|cost|price|amount|invoice)\b", lowered):
        wanted.append("money")
    if re.search(r"\b(ticket|case|ref|reference|incident)\b", lowered):
        wanted.append("case-ref")
    return wanted or ["url", "date", "time", "money", "case-ref"]


def gather(store: MailStore, msg: Message, *, cap: str | None = None) -> Evidence:
    """Collect quotable evidence for answering `msg`. Never invents anything."""
    wanted = _wants(f"{msg.subject} {msg.body}")
    evidence = Evidence(method="thread-walk")

    # 1. The thread this message sits in, earlier messages only.
    thread = store.thread_of(msg.id, reason=f"grounding {msg.id}")
    earlier = [m for m in thread if m.timestamp < msg.timestamp and m.id != msg.id]
    evidence.consulted = [m.id for m in earlier]
    _harvest(evidence, earlier, wanted)

    if evidence.grounded:
        tracing.emit(
            "retrieval", cap=cap, msg_id=msg.id, method="thread-walk",
            consulted=evidence.consulted, cited=evidence.cited_ids,
        )
        return evidence

    # 2. Nothing in the thread. Widen to keyword search across the mailbox.
    # Search broadly, quote narrowly. The topic gate uses the subject's terms,
    # not the body's, because a body shares incidental words with half the
    # mailbox: "coffee when you're back in town" and "office closed for
    # building maintenance" have "building" in common and nothing else, which
    # was enough to make m051 cite m118 as though it answered it.
    topic = keywords(msg.subject)
    if not topic:
        # A subject with no content word in it -- m012 is titled "the thing" --
        # gives nothing to match on outside its own thread. Widening the search
        # on body chatter alone finds a message sharing a word like "call" and
        # produces a confident answer to a question nobody asked. Refuse.
        tracing.emit(
            "retrieval", cap=cap, msg_id=msg.id, method="none",
            consulted=evidence.consulted, cited=[],
            why="subject carries no content word; cannot ground outside the thread",
        )
        return evidence

    terms = keywords(f"{msg.subject} {msg.body}")
    candidates = store.search(terms, exclude={msg.id} | set(evidence.consulted), limit=4)
    read = [store.get(c.id, reason=f"keyword fallback for {msg.id}") for c in candidates]
    evidence.method = "keyword"
    evidence.consulted += [m.id for m in read]
    _harvest(evidence, read, wanted, topic=topic)

    tracing.emit(
        "retrieval", cap=cap, msg_id=msg.id, method=evidence.method,
        terms=terms, consulted=evidence.consulted, cited=evidence.cited_ids,
    )
    return evidence


def _harvest(
    evidence: Evidence, messages: list[Message], wanted: list[str], topic: list[str] | None = None
) -> None:
    """Collect quotable sentences.

    `topic` guards the cross-thread case. Within a thread, any fact of the
    right shape is fair game, because the thread is the context. Outside it,
    a sentence is only evidence if it is about the same subject -- otherwise a
    request for the board deck happily quotes a ZenBoard renewal date purely
    because both contain "the 16th", which is how a system invents a detail
    while believing it cited one.
    """
    for message in messages:
        for shape, pattern in _ANSWER_SHAPES:
            if shape not in wanted:
                continue
            for match in pattern.finditer(message.body):
                snippet = _sentence_around(message.body, match.start(), match.end())
                if not snippet or (message.id, snippet) in evidence.quotes:
                    continue
                if topic and not any(term in snippet.lower() for term in topic):
                    continue
                evidence.quotes.append((message.id, snippet))


def _sentence_around(body: str, start: int, end: int) -> str:
    left = max(body.rfind(".", 0, start), body.rfind("\n", 0, start)) + 1
    right = min(
        [i for i in (body.find(".", end), body.find("\n", end)) if i != -1] or [len(body)]
    )
    return " ".join(body[left : right + 1].split()).strip()


def verify_citations(store: MailStore, cited: list[str], tracer=None) -> tuple[list[str], list[str]]:
    """Split cited ids into (verified, rejected).

    A citation survives only if the message exists and this run read it.
    """
    tracer = tracer or tracing.active()
    actually_read = tracer.read_ids() if tracer else set()
    verified, rejected = [], []
    for msg_id in cited:
        if msg_id not in store:
            rejected.append(f"{msg_id} (no such message)")
        elif msg_id not in actually_read:
            rejected.append(f"{msg_id} (never read during this run)")
        else:
            verified.append(msg_id)
    return verified, rejected
