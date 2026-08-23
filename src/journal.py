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

    def realized_loss_today(self) -> float:
        """Sum of negative `realized_pnl` fields logged today. 0.0 if none / file missing."""
        if not self.path.exists():
            return 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        loss = 0.0
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("ts", "").startswith(today) and "realized_pnl" in record:
                    pnl = record["realized_pnl"]
                    if pnl < 0:
                        loss += -pnl
        return loss
