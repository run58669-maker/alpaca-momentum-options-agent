"""Watch the idempotency key refuse a duplicate close.

The scenario is the one that makes the key necessary: the first closing order reaches
Alpaca but the response is lost, so the position is still on the book on the next pass
and the exit policy decides to close it again. Here that is simulated by pinning
`get_all_positions` to a book that keeps returning the same structure.

    py scratch/repro_duplicate_close_20260825_1120.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent import manage_exits          # noqa: E402
from exits import ExitPolicy            # noqa: E402
from journal import Journal             # noqa: E402
from mcp_client import MockAlpacaMCPClient  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        journal = Journal(Path(tmp) / "decisions.jsonl")
        async with MockAlpacaMCPClient() as client:
            book = await client.get_all_positions()
            stuck = [p for p in book if p["underlying"] == "QQQ"]  # the time-stop structure

            async def frozen_book():
                return stuck

            client.get_all_positions = frozen_book

            for attempt in (1, 2):
                (record,) = await manage_exits(client, journal, ExitPolicy())
                print(f"--- attempt {attempt} ---")
                print(f"  decision       : {record['action']}")
                print(f"  client_order_id: {record['client_order_id']}")
                print(f"  order_rejected : {record['order_rejected']}")
                print(f"  response       : {json.dumps(record['order'])[:200]}")

            print(f"\norders actually on the wire: {len(client._orders)} (positions closed once)")


asyncio.run(main())
