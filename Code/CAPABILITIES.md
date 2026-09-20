# CAPABILITIES.md — inboxHero

**Student:** G P Kalyan, evernorth-aai-1200014
**Repository:** https://github.com/gpkalyan7/IIITH_Agentic_AI

Run everything through one entry point:

```
python demo.py --cap R1        # one capability
python demo.py --all           # all of them, in the order below
python demo.py --cap R3 --dry-run
```

No dependencies to install. Python 3.10+ and the standard library are enough;
a local model is optional and the system says so when it is absent.

---

## The system, in one paragraph

A single Python pipeline, no framework. All 100 messages are routed before
anything expensive happens: hostile mail and cheap mail are settled by rule,
and only the remainder reaches a model. What survives routing goes through
dispose → record preferences → retrieve → draft → gate → extract commitments →
report. State that must outlive a run — standing preferences, the event log —
is kept in small files on disk. Message bodies enter the model's context only
as fenced, neutralised data, and the two functions that can do something
irreversible are reachable only through an approval gate.

## Design choices you were asked to state

**Framework: none.** The work is a linear pass with exactly one branch, and
that branch is the router. A crew or graph would have added a scheduler I do
not need and, more importantly, would have put a layer between me and the one
thing this assignment is really about: which component is allowed to call
which tool. The Part 6 defence is a statement about reachability in my own
call graph, and I wanted to be able to prove it by reading the code rather
than by trusting a framework's tool-permission configuration. See Final Report
Q4 in `README.md`.

**Retrieval: thread-walk, with keyword search as fallback.** An inbox already
carries a correct, human-maintained structure in `thread_id`. Walking it is
exact, costs nothing, and explains itself — "I used m003 because it is the
message before yours in this thread" is a reason a person can check. That is
also the case that matters most here: m008 asks for a URL that appears in
exactly one message of its own thread. Embeddings would have added an index,
a dependency and a similarity score, and would have been *less* precise on
that case. Keyword search covers cross-thread lookups, and it is deliberately
gated: a candidate is only quotable if the snippet shares a content word with
the requesting message's **subject**. Without that gate the system cheerfully
grounded "coffee when you're back in town?" in "office closed for building
maintenance", because both contain the word *building*. A message whose
subject has no content word at all — m012, titled "the thing" — cannot be
grounded outside its thread and is refused rather than guessed at.

**Dispositions.** `reply`, `archive`, `defer`, `delegate`, `escalate`, plus two
additions:

- `ask` — the system cannot tell what is being requested. Without it, m012
  has to be forced into `reply`, and a system that must reply will invent what
  "that thing we talked about" was.
- `quarantine` — hostile, flagged, reported, left in place. Without it,
  refusing a hostile message means archiving it, which hides it. Part 6
  requires the opposite.

**Reversible vs irreversible.** `send` and `delete` are irreversible.
`draft`, `label`, `archive`, `defer`, `flag` and `cc` are reversible and run
without asking. An archived message is still in `inbox.json` and a draft is a
file nobody has seen, so both are recoverable. `send` is irreversible because
once the owner's name is on a message in someone else's inbox, nothing
retracts it. **Deleting is irreversible in this design** because the mock
store has no trash — and beyond recoverability, a system that quietly deletes
is one whose mistakes are invisible, which is worse than the mistake. Nothing
in inboxHero ever proposes a delete; the operation is implemented anyway, and
gated, so that the claim is demonstrable rather than merely asserted.

**Where the gate sits.** Exactly two functions can cause an effect outside the
process, both in `tools.py`, and both demand a `Gate` decision whose token is
keyed to the specific proposal that was approved. Nothing else in the system
can reach them, and no agent imports `tools.py` at all. A second, independent
check sits on top: `send` refuses any recipient not already present in
`inbox.json`. That is aimed squarely at Part 6 — `archive@mail-backup-service.info`
(demanded by m024) and `finance-sync@ext-audit.co` (demanded by m047) have
never written to this mailbox, so they are unreachable *even if a human
approved the send by mistake*.

**Escalation line, and what it cost.** The gate is asked about every `send`
and every `delete`, and about nothing else. Archiving a receipt or deferring a
newsletter happens silently. On this inbox that is a handful of approvals per
run rather than forty, which is the point: a person asked to approve forty
things approves forty things without reading them. What I traded away is real
— a wrongly archived message is possible and nobody is consulted about it. I
think that is the right way round, because a wrong archive is recoverable from
`inbox.json` and a wrong send is not. The one place I went further than "is it
irreversible" is content: a draft that correctly quotes a credential is still
gated with a different reason, because re-transmitting a secret to a new
recipient is a second harm that the grounding check would otherwise wave
through.

**Preferences cannot loosen controls.** The preference store holds a closed
vocabulary of five kinds, none of which can express "skip approval" or "act
autonomously", and `gate.py` never reads it. This matters because m039 arrives
looking exactly like the legitimate m041 — a note to the assistant, apparently
from `sam@paperjet.io` — and sender identity cannot separate them. What
separates them is that "never schedule before 11:00" is representable and
"send to investors without asking for approval" is not.

**Model use.** Developed against `llama3.1:8b` on local Ollama, configured
through `config.py` from environment variables. Every agent has a
deterministic baseline that runs first; the model only refines wording and
borderline judgements, and its output is validated against a fixed JSON schema
with no field capable of naming a tool, a recipient or an action. So no safety
property depends on the model behaving, and the whole manifest still runs with
`LLM_PROVIDER=offline`. Calls are paced, retried with backoff on HTTP 429, and
cached on disk by prompt hash so a re-run is free and reproducible.

## Capabilities

| id | name | tier | one-line claim |
|----|------|------|----------------|
| R1 | Zero the inbox | B | all 100 messages get one disposition + reason; 64 never reach a model |
| R2 | Grounded reply | B | drafts cite the earlier message they used, and refuse when it isn't there |
| R3 | Gate the irreversible | C | no send or delete without approval or `--dry-run` |
| R4 | Persistent preference | C | a stated preference survives a restart and changes behaviour |
| R5 | Refuse embedded instructions | C | detects, refuses, flags and reports 4 injections and 3 phishing attempts |
| R6 | Dashboard | C | three panes, commitments cited, conflicts surfaced |
| X1 | Sender lookup | A | unread mail from one sender, one lookup, no model |
| X2 | Thread to open question | B | 9-message thread reduced to the one ask still waiting |
| X3 | Morning digest | B | needs you / can wait / handled for you |
| X4 | Follow-up tracking | B | sent mail nobody answered, with a drafted chase |
| X5 | Why did you do that? | C | replays one message's decision trail from the log on disk |

Tiers A, B and C are all represented. The exact command, observable outcome
and evidence for each is in `capabilities.json`, which is the machine-readable
version and what a marking script reads; this file is for a human. The two are
kept in step.

## What the required six actually do on this inbox

A few specifics, so the claims above are checkable without running anything:

- **R1** reports `undecided: 0` and `handled by rules, never reached a model:
  64`. The 64 are receipts, newsletters, service notifications, the seven
  hostile messages, and mail the owner sent.
- **R2** answers m008 (Devika asking for the staging queue credentials) from
  m003, the only message that contains the AMQP URL, found by thread-walk. It
  then shows the negative case: m012 produces no draft.
- **R3** under `--dry-run` prints every send it would perform and reports
  `outbox/ writes: 0`.
- **R4** records "no meetings before 11:00" from m041, and on a later run in a
  new process declines m043's Monday 9:00am request, offering 11:00, 12:00 and
  13:00, holding the reply for approval. It also shows that m039's attempt to
  store "autonomous mode" was refused.
- **R5** flags m017, m024, m039 and m047 as injections and m021, m023 and m045
  as phishing, confirms none of them were deleted, and confirms neither
  exfiltration address is in the 80-address recipient allowlist.
- **R6** puts "Board deck finished and circulated" on Wed 16 Sep citing
  **[m038, m040]** — a date stated in neither message alone, since m040 says
  "two days before the board review" and m038 says the review is the 18th —
  and raises `CONFLICT at Tue 15 Sep 15:00` between the Northwind intro call
  (m010) and the dental appointment (m061).

## Assumptions about the data

- 100 messages, 88 threads, 80 distinct correspondents, 77 unread.
- The owner is `sam@paperjet.io`. Four messages in the store were sent *by* the
  owner rather than to them, which is how follow-up tracking finds m044.
- Timestamps are naive ISO-8601 with no timezone and all fall in September
  2026, so bare day-of-month references ("the 18th", "Tuesday the 15th")
  resolve into that month. Weekday references resolve forward from the send
  date, preferring the weekday attached to a time — m013 names Thursday twice
  and Wednesday once, and the one that matters is the one carrying "2:00pm".
- Attachments are not modelled; messages refer to "the portal" instead.
- "Sending" means writing a pair of files to `outbox/`. No real mail is sent
  and no network mail service is contacted.

## Final Report

The four required answers are in `README.md`.
