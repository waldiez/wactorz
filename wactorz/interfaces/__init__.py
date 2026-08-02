"""User-facing chat interfaces (CLI, Discord, Telegram, WhatsApp).

An explicit package rather than an implicit namespace one: a directory of the
same name earlier on ``sys.path`` would otherwise merge into this namespace,
and type-checkers resolve a real package more predictably.
"""
