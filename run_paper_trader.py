#!/usr/bin/env python3
"""
Root-level launcher for the paper trading loop.

Usage (from the project root):
    python3 run_paper_trader.py
"""
import sys
import pathlib
import asyncio

sys.path.insert(0, str(pathlib.Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=pathlib.Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

from agents.backtest.paper_trader import main

if __name__ == "__main__":
    asyncio.run(main())
