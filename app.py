"""Hosted entry point (Vercel's Python preset loads `app` from app.py).

Serves the MCP server over HTTP at /mcp/<MCP_ACCESS_TOKEN>; see remote.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from espn_fantasy_hub.remote import create_app  # noqa: E402

app = create_app()
