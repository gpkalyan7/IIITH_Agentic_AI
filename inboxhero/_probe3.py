"""Scratch probe: whole pipeline, dry-run. Development aid, removed before submission."""
import config
from inboxhero import pipeline, tracing
from inboxhero.gate import Gate
from inboxhero.llm import LLMClient
from inboxhero.mailstore import MailStore
from inboxhero.memory import PreferenceStore
from inboxhero.tools import Toolbox

cfg = config.load_config()
config.ensure_dirs()
tracing.start(config.ROOT / "_probe_trace.jsonl", cap="probe", append=False)

store = MailStore(config.INBOX_PATH, config.OWNER_ADDRESS)
prefs = PreferenceStore(config.PREFS_PATH)
prefs.clear()
prefs = PreferenceStore(config.PREFS_PATH)
gate = Gate(dry_run=True)
toolbox = Toolbox(outbox_dir=config.OUTBOX_DIR, gate=gate, allowed_recipients=store.known_addresses())
client = LLMClient(cfg)

r = pipeline.run(
    store=store, prefs=prefs, gate=gate, toolbox=toolbox, client=client,
    owner=config.OWNER_ADDRESS, owner_domain=config.OWNER_DOMAIN, cap="probe",
)

print(f"model: {r.llm_status}")
print(f"decisions: {len(r.decisions)}  undecided: {len(r.undecided)}")
print(f"rule-handled (no model): {r.rule_handled}   model-eligible: {r.model_eligible}")
print(f"counts: {r.counts()}")
print(f"\nPREFERENCES RECORDED ({len(r.preferences_recorded)}):")
for p in r.preferences_recorded:
    print("  " + p)
print(f"REJECTED: {r.preferences_rejected}")
print(f"\nREFUSALS ({len(r.refusals)}):")
for f in r.refusals:
    print("  " + f.summary_line())
print(f"\nDRAFTS ({len(r.drafts)}):")
for d in r.drafts:
    tag = "GROUNDED" if d.grounded else "REFUSED "
    print(f"  {tag} {d.msg_id} cites={d.verified_cites} sensitive={bool(d.sensitive)}")
    if not d.grounded:
        print(f"           {d.refusal[:110]}")
print(f"\nSCHEDULE CHECKS ({len(r.schedule_checks)}):")
for s in r.schedule_checks:
    if s.needs_pushback:
        print(f"  {s.msg_id}: {s.violates or s.clashes_with} -> offers {s.alternatives}")
print(f"\nPENDING ({len(r.pending)}):")
for p in r.pending[:12]:
    print(f"  {p.msg_id} {p.action}: {p.why_human[:80]}")
print(f"\nCONFLICTS: {[c.when_text for c in r.conflicts]}")
print(f"outbox writes: {len(toolbox.sent)}")
