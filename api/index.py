"""Vercel entry point: serves the MCP server over HTTP (see remote.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from espn_fantasy_hub.remote import create_app  # noqa: E402

app = create_app()
