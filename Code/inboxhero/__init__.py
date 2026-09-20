"""inboxHero: an agentic system that takes an inbox from unread to empty.

The package is deliberately flat and readable. Roles a framework would name
for you are named here instead:

  router.py            the router
  agents/              the agents, one file per job
  pipeline.py          the crew: what runs, in what order
  gate.py              the only path to an action that cannot be undone
  envelope.py          the boundary between text we read and text we obey
"""

__version__ = "1.0.0"
