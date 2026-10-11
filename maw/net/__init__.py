"""net — the switchboard: session registry + push transport for live agent↔agent comms.

Layer summary (see SWITCHBOARD.md at repo root):
- registry.py — who is alive (net who): per-session JSON entries, crash-safe pruning
- transport.py — the wake line: tmux bracketed-paste delivery into a peer's live pane
- wire.py — the audit line: deliveries recorded as bridge-format turns
- cli.py — `python3 -m net.cli {register,who,send,heartbeat,unregister}`

The bridge (bridge/) stays the work line; net adds the wake line.
"""
