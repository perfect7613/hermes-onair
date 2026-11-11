"""On Air core: everything that does not depend on Hermes.

``obs`` talks to OBS, ``studio`` and ``stage`` say what to do with it, ``store`` remembers,
``director`` runs a session, ``reel`` wraps it up. The plugin package around this one adapts it
to Hermes (tools, commands, hooks, the Desktop pane).
"""
