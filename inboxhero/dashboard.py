"""The three-pane view, built from a run rather than assembled by hand.

  Pending actions   what the system wants to do but may not do alone
  Flagged           what it refused to act on, and what it did instead
  Commitments       the calendar, every entry citing its sources, conflicts
                    called out above the table rather than buried in it

Both a terminal rendering and a static HTML page come out of the same
dictionary, so the two cannot disagree, and dashboard.json is that dictionary
written to disk for anyone who wants to check the numbers.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path


def build(result, store, prefs, toolbox) -> dict:
    """Everything the three panes need, as plain data."""
    ungrounded = [d for d in result.drafts if not d.grounded]

    flagged = [r.to_dict() for r in result.refusals]
    for item in ungrounded:
        flagged.append(
            {
                "message": item.msg_id,
                "from": store.peek(item.msg_id).sender if store.peek(item.msg_id) else "",
                "subject": store.peek(item.msg_id).subject if store.peek(item.msg_id) else "",
                "kind": "ungrounded",
                "attempted": "draft a reply",
                "signals": [],
                "destinations_requested": [],
                "what_the_system_did": item.refusal,
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": result.llm_status,
        "totals": {
            "messages": len(store),
            "dispositions": result.counts(),
            "undecided": len(result.undecided),
            "rule_handled_without_model": result.rule_handled,
            "model_eligible": result.model_eligible,
            "outbox_writes": len(toolbox.sent),
        },
        "preferences": [p.to_dict() for p in prefs.all()],
        "panes": {
            "pending_actions": [p.to_dict() for p in result.pending],
            "flagged": flagged,
            "commitments": {
                "conflicts": [c.to_dict() for c in result.conflicts],
                "calendar": [c.to_dict() for c in result.calendar],
            },
        },
    }


# -- terminal ---------------------------------------------------------------


def render_text(data: dict) -> str:
    lines: list[str] = []
    add = lines.append
    totals = data["totals"]

    add("=" * 78)
    add("  inboxHero -- run dashboard")
    add(f"  generated {data['generated_at']}   model: {data['model']}")
    add("=" * 78)
    add(
        f"  {totals['messages']} messages | undecided: {totals['undecided']} | "
        f"handled by rules without a model: {totals['rule_handled_without_model']} | "
        f"outbox writes: {totals['outbox_writes']}"
    )
    add("  dispositions: " + ", ".join(f"{k}={v}" for k, v in totals["dispositions"].items()))

    add("")
    add("-" * 78)
    add(f"  PANE 1 -- PENDING ACTIONS ({len(data['panes']['pending_actions'])})")
    add("  things the system wants to do but may not do on its own")
    add("-" * 78)
    for item in data["panes"]["pending_actions"]:
        add(f"  {item['message']:6} {item['proposed_action']:14} {item['what'][:52]}")
        add(f"         needs a human because: {item['why_it_needs_a_human']}")

    add("")
    add("-" * 78)
    add(f"  PANE 2 -- FLAGGED ({len(data['panes']['flagged'])})")
    add("  things the system refused to act on")
    add("-" * 78)
    for item in data["panes"]["flagged"]:
        add(f"  {item['message']:6} [{item['kind']}] from {item['from']}")
        add(f"         attempted: {item['attempted']}")
        if item["destinations_requested"]:
            add(f"         destination asked for: {', '.join(item['destinations_requested'])}")
        add(f"         system did: {item['what_the_system_did']}")

    commitments = data["panes"]["commitments"]
    add("")
    add("-" * 78)
    add(f"  PANE 3 -- COMMITMENTS ({len(commitments['calendar'])})")
    add("-" * 78)
    if commitments["conflicts"]:
        for conflict in commitments["conflicts"]:
            add(f"  ** CONFLICT at {conflict['when']} **")
            for entry in conflict["items"]:
                add(f"       {entry['title']}  [{', '.join(entry['cites'])}]")
            add(f"       {conflict['note']}")
        add("")
    else:
        add("  no conflicts found")
        add("")
    for item in commitments["calendar"]:
        mark = " (derived from several messages)" if item["derived_from_multiple"] else ""
        add(f"  {item['when']:20} {item['title'][:42]:44} [{', '.join(item['cites'])}]{mark}")
        if item["derived_from_multiple"] and item["detail"]:
            add(f"      {item['detail']}")
    add("")
    return "\n".join(lines)


# -- html -------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 2rem;
       background: #f6f7f9; color: #1b1f24; }
h1 { margin: 0 0 .2rem; font-size: 1.5rem; }
.sub { color: #57606a; margin-bottom: 1.5rem; font-size: .9rem; }
.totals { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.5rem; }
.chip { background: #fff; border: 1px solid #d8dee4; border-radius: 999px; padding: .25rem .8rem; font-size: .85rem; }
.panes { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 1.25rem; }
section { background: #fff; border: 1px solid #d8dee4; border-radius: 10px; padding: 1.1rem 1.25rem; }
section h2 { font-size: 1rem; margin: 0 0 .1rem; }
section .count { color: #57606a; font-size: .8rem; margin-bottom: .9rem; }
.row { border-top: 1px solid #eaeef2; padding: .6rem 0; }
.row:first-of-type { border-top: 0; }
.id { font-family: ui-monospace, Menlo, Consolas, monospace; background: #eef1f4; border-radius: 4px;
      padding: .05rem .35rem; font-size: .8rem; }
.why { color: #57606a; font-size: .86rem; margin-top: .2rem; }
.did { color: #1a7f37; font-size: .86rem; margin-top: .2rem; }
.att { color: #a40e26; font-size: .86rem; margin-top: .2rem; }
.conflict { background: #fff2f0; border: 1px solid #ffb3a7; border-radius: 8px; padding: .75rem .9rem;
            margin-bottom: 1rem; }
.conflict strong { color: #a40e26; }
.derived { display: inline-block; background: #fff8c5; border: 1px solid #e6c200; border-radius: 4px;
           padding: 0 .3rem; font-size: .72rem; margin-left: .3rem; }
.when { font-weight: 600; }
.cites { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .78rem; color: #57606a; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  section, .chip { background: #161b22; border-color: #30363d; }
  .id { background: #21262d; } .why, .count, .sub, .cites { color: #8b949e; }
  .conflict { background: #2d1214; border-color: #6e2530; }
  .derived { background: #2d2a12; border-color: #7a6a00; color: #e6edf3; }
}
"""


def render_html(data: dict) -> str:
    esc = html.escape
    totals = data["totals"]
    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>inboxHero dashboard</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>inboxHero</h1>",
        f"<div class='sub'>generated {esc(data['generated_at'])} &middot; model: {esc(data['model'])}</div>",
        "<div class='totals'>",
        f"<span class='chip'>{totals['messages']} messages</span>",
        f"<span class='chip'>undecided: {totals['undecided']}</span>",
        f"<span class='chip'>rule-handled, no model: {totals['rule_handled_without_model']}</span>",
        f"<span class='chip'>outbox writes: {totals['outbox_writes']}</span>",
    ]
    for name, count in totals["dispositions"].items():
        parts.append(f"<span class='chip'>{esc(name)}: {count}</span>")
    parts.append("</div><div class='panes'>")

    # Pane 1
    pending = data["panes"]["pending_actions"]
    parts.append(
        f"<section><h2>Pending actions</h2><div class='count'>{len(pending)} "
        "things the system wants to do but may not do alone</div>"
    )
    for item in pending:
        parts.append(
            f"<div class='row'><span class='id'>{esc(item['message'])}</span> "
            f"<strong>{esc(item['proposed_action'])}</strong><br>{esc(item['what'])}"
            f"<div class='why'>needs a human because: {esc(item['why_it_needs_a_human'])}</div></div>"
        )
    parts.append("</section>")

    # Pane 2
    flagged = data["panes"]["flagged"]
    parts.append(
        f"<section><h2>Flagged</h2><div class='count'>{len(flagged)} "
        "things the system refused to act on</div>"
    )
    for item in flagged:
        dest = (
            f"<div class='att'>destination asked for: {esc(', '.join(item['destinations_requested']))}</div>"
            if item["destinations_requested"]
            else ""
        )
        parts.append(
            f"<div class='row'><span class='id'>{esc(item['message'])}</span> "
            f"<strong>{esc(item['kind'])}</strong> &mdash; {esc(item['from'])}<br>"
            f"{esc(item['subject'])}"
            f"<div class='att'>attempted: {esc(item['attempted'])}</div>{dest}"
            f"<div class='did'>{esc(item['what_the_system_did'])}</div></div>"
        )
    parts.append("</section>")

    # Pane 3
    commitments = data["panes"]["commitments"]
    parts.append(
        f"<section><h2>Commitments</h2><div class='count'>{len(commitments['calendar'])} "
        "dates and obligations, each cited to its source</div>"
    )
    for conflict in commitments["conflicts"]:
        rows = "".join(
            f"<div>{esc(e['title'])} <span class='cites'>[{esc(', '.join(e['cites']))}]</span></div>"
            for e in conflict["items"]
        )
        parts.append(
            f"<div class='conflict'><strong>CONFLICT at {esc(conflict['when'])}</strong>"
            f"{rows}<div class='why'>{esc(conflict['note'])}</div></div>"
        )
    for item in commitments["calendar"]:
        badge = "<span class='derived'>derived from several messages</span>" if item["derived_from_multiple"] else ""
        detail = (
            f"<div class='why'>{esc(item['detail'])}</div>"
            if item["derived_from_multiple"] and item["detail"]
            else ""
        )
        parts.append(
            f"<div class='row'><span class='when'>{esc(item['when'])}</span>{badge}<br>"
            f"{esc(item['title'])} <span class='cites'>[{esc(', '.join(item['cites']))}]</span>"
            f"{detail}</div>"
        )
    parts.append("</section></div></body></html>")
    return "".join(parts)


def write(data: dict, html_path: Path, json_path: Path) -> None:
    Path(html_path).write_text(render_html(data), encoding="utf-8")
    Path(json_path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
