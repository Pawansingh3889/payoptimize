"""The resident ops agent: an LLM that reads this system through redacted,
tenant-scoped tools and mutates it only through a guarded whitelist.

The package layering mirrors the rest of the service. `privacy` builds the
boundary, `llm` refuses to cross it, `tools` are the only eyes, `actions` are
the only hands, `loop` is the conversation, and `triggers` decide when it
happens without being asked.
"""
