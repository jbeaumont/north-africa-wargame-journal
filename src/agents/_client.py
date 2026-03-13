"""
Shared Anthropic client factory.

In a standard environment the SDK reads ANTHROPIC_API_KEY automatically.
In Claude Code remote environments the SDK key is empty but a session ingress
token is available via CLAUDE_SESSION_INGRESS_TOKEN_FILE; that token requires
Bearer (auth_token) authentication rather than X-Api-Key.
"""
from __future__ import annotations

import os
from pathlib import Path

import anthropic


def make_client() -> anthropic.Anthropic:
    """Return an authenticated Anthropic client for the current environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)

    # Claude Code remote: try the session ingress token file (Bearer auth)
    token_file = os.environ.get(
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
        "/home/claude/.claude/remote/.session_ingress_token",
    )
    try:
        token = Path(token_file).read_text().strip()
        if token:
            return anthropic.Anthropic(auth_token=token)
    except (FileNotFoundError, IOError):
        pass

    # Let the SDK raise its own auth error with a clear message
    return anthropic.Anthropic()
