"""One module per unit of reasoning.

Each agent takes messages in and returns a validated dataclass. None of them
can perform an action: they have no access to tools.py and no way to reach it.
Acting on what an agent returns is pipeline.py's job, and anything irreversible
goes through gate.py on the way.
"""
