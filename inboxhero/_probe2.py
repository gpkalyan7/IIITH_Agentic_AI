"""Scratch probe: commitments + conflicts. Development aid, removed before submission."""
import config
from inboxhero import tracing, rules
from inboxhero.mailstore import MailStore
from inboxhero.agents import commitments

config.ensure_dirs()
tracing.start(config.ROOT / "_probe_trace.jsonl", cap="probe", append=False)
store = MailStore(config.INBOX_PATH, config.OWNER_ADDRESS)

# Only non-hostile, non-noise messages feed the calendar.
candidates = []
for m in store.all():
    if rules.classify_threat(m, config.OWNER_DOMAIN).hostile:
        continue
    if rules.cheap_disposition(m):
        continue
    candidates.append(store.get(m.id, reason="commitment scan"))

items, conflicts = commitments.extract(store, candidates, cap="probe")

print(f"COMMITMENTS ({len(items)}):")
for c in items:
    mark = "  <-- DERIVED FROM MULTIPLE" if c.derived and len(c.cites) > 1 else ""
    print(f"  {c.when_text:22} {c.title:44} {c.cites}{mark}")

print(f"\nCONFLICTS ({len(conflicts)}):")
for c in conflicts:
    print(f"  {c.when_text}: " + " vs ".join(f"{i.title} {i.cites}" for i in c.items))
