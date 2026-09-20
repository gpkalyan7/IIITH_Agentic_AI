"""Scratch probe: sanity-check the rule layer against the real inbox.

Development aid, not part of the system. Removed before submission.
"""
import config
from inboxhero.mailstore import MailStore
from inboxhero import rules

store = MailStore(config.INBOX_PATH, config.OWNER_ADDRESS)
print(f"messages: {len(store)}")

hostile, cheap, to_model = [], [], []
for m in store.all():
    v = rules.classify_threat(m, config.OWNER_DOMAIN)
    if v.hostile:
        hostile.append((m, v))
        continue
    if rules.cheap_disposition(m):
        cheap.append(m)
    else:
        to_model.append(m)

print(f"\nHOSTILE ({len(hostile)}):")
for m, v in hostile:
    print(f"  {m.id} [{v.kind:9}] {m.sender:42} {v.signal_names}")
    print(f"        attempted: {v.attempted}")

print(f"\nRULE-HANDLED CHEAP: {len(cheap)}")
print(f"TO MODEL: {len(to_model)}")
for m in to_model:
    print(f"  {m.id} {m.sender:42} {m.subject[:52]}")
print(f"\ntotal: {len(hostile) + len(cheap) + len(to_model)}")
