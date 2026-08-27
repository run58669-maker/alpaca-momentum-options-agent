"""Append-only JSONL decision journal. One line per decision, human-readable reason included."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, **fields: Any) -> dict[str, Any]:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record

    def logged_closing_ids(self) -> set[str]:
        """Every `closing_activity_id` already journalled, across all days.

        Reconciliation runs on every pass over an overlapping window of fills, so
        this is what stops one closed position from being counted as a fresh loss
        again and again until the circuit breaker trips on nothing.
        """
        ids: set[str] = set()
        for record in self._records():
            activity_id = record.get("closing_activity_id")
            if activity_id:
                ids.add(str(activity_id))
        return ids

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def realized_loss_today(self) -> float:
        """Sum of negative `realized_pnl` fields logged today. 0.0 if none / file missing."""
        today = datetime.now(timezone.utc).date().isoformat()
        loss = 0.0
        for record in self._records():
            if record.get("ts", "").startswith(today) and "realized_pnl" in record:
                pnl = record["realized_pnl"]
                if pnl < 0:
                    loss += -pnl
        return loss
