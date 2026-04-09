#!/usr/bin/env python3
"""
Root-level launcher for the Agentic Trading Terminal API server.

Usage (from the project root):
    python3 run_server.py

This is equivalent to:
    uvicorn agents.api_server:app --host 0.0.0.0 --port 8000
"""
import sys
import pathlib

# Ensure the project root is always on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "agents.api_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
