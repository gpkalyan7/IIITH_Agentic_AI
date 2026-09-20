#!/usr/bin/env python3
"""inboxHero -- one entry point for every capability in the manifest.

    python demo.py --cap R1              one capability
    python demo.py --all                 all of them, in manifest order
    python demo.py --cap R3 --dry-run    show what would be sent, send nothing

Every command in CAPABILITIES.md is a line you can run here. Nothing in this
file performs an irreversible action itself; it asks the pipeline, and the
pipeline asks the gate.
"""

from __future__ import annotations

import argparse
import json
import sys

import config
from inboxhero import dashboard, pipeline, tracing
from inboxhero.agents import extras, grounder, scheduler, sentinel
from inboxhero.gate import Gate
from inboxhero.llm import LLMClient
from inboxhero.mailstore import MailStore
from inboxhero.memory import PreferenceStore
from inboxhero.tools import Toolbox

CAPABILITIES = ["R1", "R2", "R3", "R4", "R5", "R6", "X1", "X2", "X3", "X4", "X5"]


# -- presentation ------------------------------------------------------------


def banner(cap: str, title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {cap} -- {title}")
    print("=" * 78)


def dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# -- shared setup ------------------------------------------------------------


class Session:
    """One run's worth of wiring, built fresh so nothing leaks between caps."""

    def __init__(self, args, cap: str):
        self.cfg = config.load_config()
        config.ensure_dirs()
        self.tracer = tracing.start(config.TRACE_PATH, cap=cap, append=not args.fresh_trace)
        self.store = MailStore(config.INBOX_PATH, config.OWNER_ADDRESS)
        self.prefs = PreferenceStore(config.PREFS_PATH)
        self.gate = Gate(
            dry_run=args.dry_run,
            interactive=not args.no_input,
            answers=args.approve or None,
        )
        self.toolbox = Toolbox(
            outbox_dir=config.OUTBOX_DIR,
            gate=self.gate,
            allowed_recipients=self.store.known_addresses(),
        )
        self.client = LLMClient(self.cfg)
        self.args = args

    def run_pipeline(self, cap: str, *, send: bool = True):
        return pipeline.run(
            store=self.store,
            prefs=self.prefs,
            gate=self.gate,
            toolbox=self.toolbox,
            client=self.client,
            owner=config.OWNER_ADDRESS,
            owner_domain=config.OWNER_DOMAIN,
            cap=cap,
            use_model=not self.args.no_model,
            send_approved=send,
        )


# -- the required six --------------------------------------------------------


def cap_R1(session) -> None:
    banner("R1", "Zero the inbox -- a disposition and a reason for every message")
    result = session.run_pipeline("R1", send=False)
    print(f"model: {result.llm_status}\n")
    print(f"{'id':6} {'disposition':12} {'by':6} reason")
    print("-" * 78)
    for decision in sorted(result.decisions, key=lambda d: d.msg_id):
        print(f"{decision.msg_id:6} {decision.disposition:12} {decision.decided_by:6} {decision.reason[:46]}")
    print("-" * 78)
    print(f"messages: {len(result.decisions)}")
    print(f"undecided: {len(result.undecided)}")
    print(f"handled by rules, never reached a model: {result.rule_handled}")
    print(f"eligible for a model call: {result.model_eligible}")
    print("counts: " + ", ".join(f"{k}={v}" for k, v in result.counts().items()))
    config.DECISIONS_PATH.write_text(
        json.dumps(
            {
                "messages_processed": len(result.decisions),
                "undecided": result.undecided,
                "rule_handled_without_model": result.rule_handled,
                "model_eligible": result.model_eligible,
                "counts": result.counts(),
                "decisions": [
                    {
                        "id": d.msg_id,
                        "disposition": d.disposition,
                        "reason": d.reason,
                        "decided_by": d.decided_by,
                    }
                    for d in sorted(result.decisions, key=lambda d: d.msg_id)
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {config.DECISIONS_PATH.name}")


def cap_R2(session, msg_id: str = "m008") -> None:
    banner("R2", f"Grounded reply -- answering {msg_id} from an earlier message")
    msg = session.store.get(msg_id, reason="R2 target")
    print(f"The message:\n  from {msg.sender}\n  {msg.subject}\n  {msg.preview(150)}\n")
    outcome = grounder.draft_reply(session.store, msg, session.prefs, session.client, cap="R2")
    if not outcome.grounded:
        print("NO DRAFT WRITTEN")
        print(f"  {outcome.refusal}")
        return
    print(f"retrieval: {outcome.method}")
    print(f"consulted: {', '.join(outcome.consulted)}")
    print(f"cited (verified against the mail store and this run's reads): {', '.join(outcome.verified_cites)}")
    if outcome.rejected_cites:
        print(f"rejected citations: {', '.join(outcome.rejected_cites)}")
    print()
    print(outcome.draft.as_text())
    if outcome.sensitive:
        print(f"\nNOTE: {outcome.sensitive}")

    print("\n-- the negative case: a message nothing in the inbox can answer --")
    vague = session.store.get("m012", reason="R2 ungroundable control")
    print(f"  m012 from {vague.sender}: {vague.preview(90)}")
    control = grounder.draft_reply(session.store, vague, session.prefs, session.client, cap="R2")
    print(f"  grounded: {control.grounded}")
    print(f"  {control.refusal}")


def cap_R3(session) -> None:
    mode = "DRY RUN" if session.gate.dry_run else "PER-ACTION APPROVAL"
    banner("R3", f"Gate the irreversible -- {mode}")
    print("irreversible: send, delete    reversible: draft, label, archive, defer, flag, cc")
    print("A send writes to outbox/ and nowhere else. Nothing proposes a delete.\n")
    result = session.run_pipeline("R3")
    print()
    print(f"proposals that needed a human: {len(session.gate.pending) + len(session.gate.performed)}")
    print(f"approved and performed:        {len(session.gate.performed)}")
    print(f"not performed:                 {len(session.gate.pending)}")
    print(f"outbox/ writes:                {len(session.toolbox.sent)}")
    print(f"deletes:                       {len(session.toolbox.deleted)}")
    for decision in session.gate.decisions:
        if decision.proposal.is_irreversible:
            print(
                f"  {decision.proposal.msg_id:6} {decision.proposal.action:7} "
                f"mode={decision.mode:9} answer='{decision.answer.strip()}' -> {decision.outcome}"
            )


def cap_R4(session) -> None:
    banner("R4", "Standing preferences that survive a restart")
    print("This command shows the stored state and applies it. To prove it survives")
    print("a restart, run it twice: the first run records, the second only reads.\n")

    before = session.prefs.all()
    if before:
        print(f"preferences already on disk at {config.PREFS_PATH.name} (written by an earlier process):")
        for pref in before:
            print(f"  {pref.key}: {pref.description}   [stated in {pref.source_msg}, recorded {pref.recorded_at}]")
    else:
        print("no preferences on disk yet; this run will record them")

    result = session.run_pipeline("R4", send=False)
    print()
    for line in result.preferences_recorded:
        print(f"  recorded: {line}")
    for line in result.preferences_rejected:
        print(f"  REJECTED: {line}")

    print("\n-- the preference changing behaviour --")
    m043 = session.store.get("m043", reason="R4 demonstration")
    print(f"  m043 from {m043.sender}: {m043.preview(110)}")
    check = scheduler.check(m043, session.prefs, result.calendar, cap="R4")
    if check.violates:
        print(f"  DECLINED: {check.violates}")
        print(f"  because of preference '{check.preference_key}' stated in {check.preference_source}")
        print(f"  offering instead: {', '.join(check.alternatives)}")
        print("  the reply is held for approval, not sent")
    else:
        print("  no preference applied (was the preference recorded?)")

    print("\n-- the hostile preference, for contrast --")
    m039 = session.store.get("m039", reason="R4 hostile preference")
    print(f"  m039 claims to be from {m039.sender} and asks to disable approval and persist that.")
    print(f"  stored? {'YES -- BUG' if session.prefs.get('autonomous_mode') else 'no'}")
    print(f"  preference keys on disk: {[p.key for p in session.prefs.all()]}")


def cap_R5(session) -> None:
    banner("R5", "The hostile inbox -- refuse, flag, report, do not delete")
    refusals = sentinel.scan(session.store, config.OWNER_DOMAIN, cap="R5")
    injections = [r for r in refusals if r.kind == "injection"]
    phishing = [r for r in refusals if r.kind == "phishing"]

    print(f"found {len(refusals)} hostile messages: {len(injections)} prompt injections, {len(phishing)} phishing\n")
    for refusal in refusals:
        print(f"FLAGGED: {refusal.msg_id} ({refusal.kind}) from {refusal.sender}")
        print(f"  subject:   {refusal.subject}")
        print(f"  attempted: {refusal.attempted}")
        print(f"  signals:   {', '.join(refusal.signals)}")
        if refusal.destinations:
            print(f"  wanted mail sent to: {', '.join(refusal.destinations)}")
        print(f"  system did: {refusal.did_instead}")
        print()

    outbox_files = sorted(p.name for p in config.OUTBOX_DIR.glob("*")) if config.OUTBOX_DIR.exists() else []
    bad_destinations = {d for r in refusals for d in r.destinations}
    leaked = [f for f in outbox_files if any(d.split("@")[0] in f for d in bad_destinations)]
    print(f"outbox/ contains {len(outbox_files)} files; messages to any requested destination: {len(leaked)}")
    print(f"still present in the mail store: {all(r.msg_id in session.store for r in refusals)}")
    print(f"deleted by this run: {len(session.toolbox.deleted)}")
    print(f"\nallowed recipients are the {len(session.store.known_addresses())} addresses already in the mailbox;")
    for dest in sorted(bad_destinations):
        print(f"  {dest} in allowlist? {dest in session.store.known_addresses()}")


def cap_R6(session) -> None:
    banner("R6", "Dashboard -- pending actions, flagged, commitments")
    result = session.run_pipeline("R6", send=False)
    data = dashboard.build(result, session.store, session.prefs, session.toolbox)
    dashboard.write(data, config.DASHBOARD_HTML, config.DASHBOARD_JSON)
    print(dashboard.render_text(data))
    print(f"wrote {config.DASHBOARD_HTML.name} and {config.DASHBOARD_JSON.name}")


# -- ours --------------------------------------------------------------------


def cap_X1(session, sender: str = "hartwellcho.com") -> None:
    banner("X1", f"Sender lookup -- unread mail from '{sender}'")
    dump(extras.sender_lookup(session.store, sender, cap="X1"))


def cap_X2(session, thread: str = "t-launch") -> None:
    banner("X2", f"Thread summary and the open question -- {thread}")
    dump(extras.thread_open_question(session.store, thread, config.OWNER_ADDRESS, session.client, cap="X2"))


def cap_X3(session) -> None:
    banner("X3", "Morning digest -- needs you / can wait / handled for you")
    result = session.run_pipeline("X3", send=False)
    dump(extras.digest(result, session.store, cap="X3"))


def cap_X4(session) -> None:
    banner("X4", "Follow-up tracking -- sent mail nobody answered")
    dump(extras.follow_ups(session.store, config.OWNER_ADDRESS, cap="X4"))


def cap_X5(session, msg_id: str = "m008") -> None:
    banner("X5", f"Why did you do that? -- the provenance of {msg_id}")
    print("Reconstructed from trace.jsonl on disk, not from this process's memory.")
    print("Run a capability first (for example --cap R3) so there is a trace to read.\n")
    explanation = extras.explain(config.TRACE_PATH, msg_id, cap="X5")
    if not explanation.steps:
        print(f"nothing in {config.TRACE_PATH.name} mentions {msg_id} yet.")
        return
    for step in explanation.steps:
        print("  " + step)
    print(f"\n{len(explanation.raw)} raw events; the full records are in {config.TRACE_PATH.name}")


# -- dispatch ----------------------------------------------------------------

HANDLERS = {
    "R1": cap_R1, "R2": cap_R2, "R3": cap_R3, "R4": cap_R4, "R5": cap_R5, "R6": cap_R6,
    "X1": cap_X1, "X2": cap_X2, "X3": cap_X3, "X4": cap_X4, "X5": cap_X5,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="inboxHero")
    parser.add_argument("--cap", choices=CAPABILITIES, help="run one capability")
    parser.add_argument("--all", action="store_true", help="run every capability in order")
    parser.add_argument("--dry-run", action="store_true", help="show irreversible actions, perform none")
    parser.add_argument("--msg", help="target message id (R2, X5)")
    parser.add_argument("--thread", help="target thread id (X2)")
    parser.add_argument("--from", dest="sender", help="sender to look up (X1)")
    parser.add_argument("--approve", action="append", help="scripted answer to the next gate prompt; repeatable")
    parser.add_argument("--no-input", action="store_true", help="never prompt; deny anything needing approval")
    parser.add_argument("--no-model", action="store_true", help="rules and baselines only")
    parser.add_argument("--fresh-trace", action="store_true", help="truncate trace.jsonl first")
    args = parser.parse_args(argv)

    if not args.cap and not args.all:
        parser.print_help()
        return 2

    caps = CAPABILITIES if args.all else [args.cap]
    for index, cap in enumerate(caps):
        session = Session(args, cap)
        handler = HANDLERS[cap]
        if cap in ("R2", "X5") and args.msg:
            handler(session, args.msg)
        elif cap == "X2" and args.thread:
            handler(session, args.thread)
        elif cap == "X1" and args.sender:
            handler(session, args.sender)
        else:
            handler(session)
        if index == 0:
            args.fresh_trace = False  # only the first capability may truncate
    return 0


if __name__ == "__main__":
    sys.exit(main())
