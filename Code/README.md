# inboxHero

**Repository: https://github.com/gpkalyan7/IIITH_Agentic_AI**

**Gurram Pavan Kalyan - evernorth-aai-1200014**

An agentic system that takes a 100-message inbox from unread to empty by
deciding what to do with every message, doing the parts it should do, and
refusing the parts it should not.

---

## Running it

```
python demo.py --cap R1              # one capability
python demo.py --all                 # all eleven, in manifest order
python demo.py --cap R3 --dry-run    # show every irreversible action, perform none
python demo.py --cap R2 --msg m008   # target a specific message
```

Python 3.10 or newer. **No packages to install** - the project uses only the
standard library, so it runs from a clean checkout immediately.

Copy `.env.example` to `.env` to point at one:

```
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1:8b
OLLAMA_HOST=http://localhost:11434
```

If no model is reachable, every capability still runs and reports
`model: unavailable ... deterministic baselines only`. That is a deliberate
property, not a fallback bolted on: see Q2 below.

## Architecture

```
demo.py                 one entry point; the --cap interface the manifest depends on
config.py               the only module that reads the environment
inboxhero/
  mailstore.py          read-only inbox; every read emits an event
  envelope.py           the trust boundary: untrusted data, fenced and neutralised
  rules.py              deterministic noise, injection and phishing detectors
  router.py             hostile | cheap | workflow | agentic
  retrieval.py          thread-walk, keyword fallback, citation verification
  memory.py             standing preferences, closed vocabulary, on disk
  gate.py               approval / dry-run; the only route to an irreversible action
  tools.py              send + delete, plus the outbound recipient allowlist
  llm.py                Ollama/Gemini/OpenAI, backoff, cache, schema-validated output
  pipeline.py           the orchestrator
  dashboard.py          three panes, terminal and HTML, from one dictionary
  agents/
    triage.py           disposition + reason
    grounder.py         drafts that can only assert what the mailbox says
    commitments.py      calendar, multi-message derivation, conflicts
    scheduler.py        meeting requests checked against stored preferences
    sentinel.py         hostile verdict -> logged, reported refusal
    extras.py           the five Part 8 capabilities
```

The flow is: **load -> route -> dispose -> record preferences -> retrieve and
draft -> gate -> extract commitments -> report.** Preferences are recorded before
drafting so a preference stated in this mailbox affects this run as well as
later ones.

Design rationale - framework choice, retrieval method, the reversible and
irreversible classification, where the gate sits, and where the escalation
line was drawn - is in **`CAPABILITIES.md`**, together with the capability
list. `capabilities.json` is the machine-readable version.

## Outputs

| file | what it is |
|---|---|
| `outbox/` | one `.eml` and one `.json` per sent message; the only place sending writes |
| `trace.jsonl` | append-only event log; every claim in the manifest is checkable against it |
| `dashboard.html` / `.json` | the three-pane view |
| `decisions.json` | every message with its disposition and reason |
| `data/prefs.json` | standing preferences, the state that survives a restart |

---

# Final Report

## 1. What did you refuse to automate?

**m008.** Devika writes into the staging thread asking Sam to "just resend the
URL you gave Raghav earlier". The system handles this almost completely: it
walks the thread, finds that m003 is the only message containing the staging
AMQP URL, and drafts a reply grounded in it with the citation verified against
the mail store. Then it stops and asks a human.

It stops because of a check that is separate from grounding. `grounder.py`
tests the quoted evidence against a pattern for embedded secrets, and
`amqp://pj_stage:Rk7-quiet-otter-51@broker-stg.paperjet.io:5672/pjs` is a live
credential. The draft is *correct* - that really is the URL, it really is in
m003, and Devika really did ask for it. Correctness is not the question. The
question is that sending it re-transmits a working secret to a third party,
and the system cannot evaluate the thing that actually matters: whether Devika
should have it. She is a colleague on the same domain and her request is
plausible, which is exactly what a convincing request from a compromised
account also looks like. Sam knows whether Devika is setting up a worker box.
The system knows only that somebody claiming to be Devika says so.

I drew the line there rather than at "external recipients" because m008 is
internal and would have sailed through a recipient-based rule. The line that
works is about what is *in* the message, not who it goes to.

## 2. Where does untrusted text enter your system?

Every message body enters through one function, `envelope.wrap()`, and it is
the only path by which mail content reaches a model. Three properties hold
there, and none of them is a sentence asking the model to behave.

**Framing.** Content is fenced inside a delimiter carrying a nonce generated
at process start. A message written yesterday cannot contain today's nonce, so
it cannot forge the end of the fence and appear to speak from outside it.

**Neutralisation.** Text resembling a fence, a role header, or a directive
aimed at an assistant is rewritten before the model sees it. The model learns
that an instruction was present - which is what allows it to be reported -
without seeing it in a form that reads as one.

**Output shape.** This is the load-bearing one. Every prompt asks for a JSON
object matching a fixed schema, and `llm.py` discards any field that is
missing or of the wrong type. **No schema in this system has a field that
names a tool, a recipient, or an action verb outside a closed vocabulary.**
The model returns data; Python decides what to do with it. So the very best a
successful injection can achieve is a wrong disposition or a clumsy sentence
in a draft. It cannot reach a send, because there is no value it can return
that a send is derived from.

**What an attacker would have to defeat.** Four things, in order, and not one
of them is a prompt:

1. `rules.py`, which classifies hostile mail deterministically and cannot be
   argued with, because it does not read for meaning. Detection deliberately
   does not use the model: asking the model whether a message is an attack is
   asking the attacker's own text to describe itself.
2. The schema boundary in `llm.py` - they would need a field to exist that
   does not.
3. `gate.py`, which every irreversible action passes through, and which no
   agent can reach.
4. The recipient allowlist in `tools.py`. Outbound mail can only go to an
   address already in `inbox.json`, so they would have to get their address
   into the mail store first - and then still pass a human.


## 3. Who is accountable when it sends the wrong thing?

**The owner is accountable.** The message goes out in Sam's name, and nothing
reaches `outbox/` without Sam having been shown the recipient, the subject,
the body, the citations and the reason it was gated, and having typed `y`. The
system is built so that there is always a human answer on the record for every
irreversible act - that is the entire purpose of `gate.py`, and it is why
`--dry-run` exists as a way to look without that record being created.

That said, "the human clicked yes" is a cheap answer if the system cannot show
what the human was told. So the real accountability mechanism is
reconstruction. `trace.jsonl` records, for every message: how it was routed
and why, every message id that was read and for what reason, the disposition
with its reason and whether a rule or the model chose it, the retrieval method
and what it consulted, the draft with its citations, the exact proposal put to
the gate with the reason it was gated, the human's literal answer, and the
outcome.

## 4. Name your own machinery

| a framework would call it | here it is | what it does |
|---|---|---|
| Agents | `agents/*.py` | one module per unit of reasoning: triage, grounder, commitments, scheduler, sentinel |
| Tasks | the functions those modules expose | each takes messages and returns a validated dataclass, never an action |
| Crew / orchestrator | `pipeline.run()` | fixes what runs, in what order, and what is carried between steps |
| Router | `router.py` | hostile / cheap / workflow / agentic, decided before anything expensive |
| Tool registry | `tools.py` | the only functions with an external effect |
| Memory | `memory.py` | standing preferences, on disk, closed vocabulary |
| Callbacks / observability | `tracing.py` | the append-only event log |

**What I built that a framework would have given me.** The retry, backoff,
pacing and response-cache layer in `llm.py`. LangChain or CrewAI would have
handed me all of that, and writing it cost me perhaps eighty lines I did not
strictly need to write.

**Would a framework have helped?** For that piece, yes. For this assignment,
no, and I think the trade was clearly worth it.

The reason is that the interesting claim in this system is a *negative* one:
that no path exists from a message body to an irreversible action except
through the gate. I can support that by reading my own code - `tools.py` is
imported by `pipeline.py` and nothing else, no module in `agents/` imports it,
and both irreversible functions begin by checking a gate token keyed to a
specific proposal. Under CrewAI that claim becomes a statement about the
framework's tool-permission model, agent delegation and whatever the executor
does when a model emits a malformed tool call - a much larger surface, most of
which I would be trusting rather than checking. Agent frameworks are built
around letting a model choose tools, and the central design decision here is
that the model is never allowed to choose one.

There is a second, smaller reason. Roughly two thirds of this inbox needs no
model at all, and the routing decision that establishes this happens before
any agent is constructed. A framework whose unit of work is "an agent with
tools" makes the cheap path the awkward case. Here it is the default, and 64
of 100 messages take it.
