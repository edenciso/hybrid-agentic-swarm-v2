"""Layer 4 — observability spine.

A deliberately small, dependency-free stand-in for the OpenTelemetry SDK +
Collector described in the reference architecture. Every event carries the
correlation ids that make forensic replay possible (mission_id, trace_id,
actor ids, causation ids). Events are kept in memory and appended to a
JSONL file so a run can be replayed after the fact.

In production, replace this module with real OTel instrumentation: the
event names here (``agent.action``, ``field.update``, ``policy.decision``,
``reward.evaluate``, ``breaker.state``, ``memory.write``) map one-to-one
onto the v1/v2 telemetry event contracts.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import new_id


class Telemetry:
    def __init__(self, mission_id: str, out_dir: str | Path = "runs") -> None:
        self.mission_id = mission_id
        self.trace_id = new_id("trc")
        self.events: list[dict[str, Any]] = []
        self._counters: dict[str, int] = {}
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / f"{mission_id}.jsonl"

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {
            "event_type": event_type,
            "timestamp": time.time(),
            "trace_id": self.trace_id,
            "span_id": new_id("spn"),
            "mission_id": self.mission_id,
            **payload,
        }
        self.events.append(event)
        self._counters[event_type] = self._counters.get(event_type, 0) + 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def count(self, event_type: str) -> int:
        return self._counters.get(event_type, 0)

    def summary(self) -> dict[str, int]:
        return dict(sorted(self._counters.items()))
