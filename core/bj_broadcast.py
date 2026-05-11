"""
Shared BlackJack WebSocket broadcast state.
Imported by both api/main.py and bot/bot.py to avoid circular imports.
"""
from __future__ import annotations

# token → set of WebSocket clients
_bj_clients: dict[str, set] = {}
