"""Layer 2 — the stigmergic coordination substrate.

A spatiotemporal, decaying, multi-channel field over a problem-defined
topology (a property graph here: nodes + edges). All inter-agent influence
flows through this one store, which makes it the single choke point to
instrument and defend.

Three operators run continuously:

* **deposit / reinforce / inhibit** — workers add signal
* **evaporation** — exponential decay so stale information self-erases
* **diffusion** — a smoothing kernel spreads signal to graph neighbours,
  creating gradients workers can climb

Poisoning defenses (v2, from doc-2 Layer 4):

* per-update delta bound (``max_field_delta_per_update``)
* per-worker deposit rate limit inside a sliding window
* provenance on every deposit (worker_id + causation_id, kept in the
  audit trail via telemetry)
* saturation guard: channels are clamped to [0, 1]
* the ``risk`` channel cannot be *reduced* by non-sentinel workers
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from .contracts import FIELD_CHANNELS, FieldSnapshot, FieldUpdate, PolicyEnvelope
from .telemetry import Telemetry


class DepositRejected(Exception):
    """Raised when a field update violates a substrate invariant."""


class PheromoneField:
    def __init__(
        self,
        swarm_mission_id: str,
        nodes: list[str],
        edges: list[tuple[str, str]],
        envelope: PolicyEnvelope,
        telemetry: Telemetry,
        decay_rate: float = 0.02,       # per decay step, exponential
        diffusion_rate: float = 0.05,   # fraction spread to neighbours
        diffusing_channels: tuple[str, ...] = ("success", "risk", "congestion"),
    ) -> None:
        self.swarm_mission_id = swarm_mission_id
        self.envelope = envelope
        self.telemetry = telemetry
        self.decay_rate = decay_rate
        self.diffusion_rate = diffusion_rate
        self.diffusing_channels = diffusing_channels
        self.closed = False

        self.nodes: dict[str, dict[str, float]] = {
            n: {c: 0.0 for c in FIELD_CHANNELS} for n in nodes
        }
        self.edges: dict[tuple[str, str], dict[str, float]] = {
            e: {c: 0.0 for c in FIELD_CHANNELS} for e in edges
        }
        self._neighbours: dict[str, list[str]] = defaultdict(list)
        for a, b in edges:
            self._neighbours[a].append(b)
            self._neighbours[b].append(a)

        # sliding-window deposit counter per worker (poisoning defense)
        self._deposit_log: dict[str, deque[float]] = defaultdict(deque)

    # -- write path ---------------------------------------------------------

    def apply(self, update: FieldUpdate, worker_role: str = "worker") -> str:
        """Apply a FieldUpdate, enforcing substrate invariants."""
        if self.closed:
            raise DepositRejected("mission namespace is closed")

        self._rate_limit(update.worker_id)

        inhibit = update.update_type == "inhibit"
        for channel, delta in update.channel_deltas.items():
            if channel not in FIELD_CHANNELS:
                raise DepositRejected(f"unknown channel {channel!r}")
            if abs(delta) > self.envelope.max_field_delta_per_update:
                raise DepositRejected(
                    f"delta {delta:+.3f} on {channel!r} exceeds bound "
                    f"±{self.envelope.max_field_delta_per_update}"
                )
            effective = -delta if inhibit else delta
            if channel == "risk" and effective < 0 and worker_role != "sentinel":
                raise DepositRejected(
                    "risk channel cannot be reduced by non-sentinel workers"
                )

        cell = self._cell(update.target_kind, update.target)
        sign = -1.0 if update.update_type == "inhibit" else 1.0
        for channel, delta in update.channel_deltas.items():
            if sign > 0:
                # Saturate guard: positive deposits lose effectiveness as a
                # channel approaches its ceiling, so runaway reinforcement
                # asymptotes instead of pinning the field (MAX-MIN-style).
                effective = delta * (1.0 - cell[channel])
                cell[channel] = _clamp(cell[channel] + effective)
            else:
                cell[channel] = _clamp(cell[channel] - delta)

        self.telemetry.emit(
            "field.update",
            swarm_mission_id=self.swarm_mission_id,
            worker_id=update.worker_id,
            field_update_id=update.field_update_id,
            update_type=update.update_type,
            target=list(update.target),
            channel_deltas=update.channel_deltas,
            causation_id=update.causation_id,
        )
        return update.field_update_id

    def _rate_limit(self, worker_id: str) -> None:
        now = time.monotonic()
        window = self.envelope.deposit_window_sec
        log = self._deposit_log[worker_id]
        while log and now - log[0] > window:
            log.popleft()
        if len(log) >= self.envelope.max_deposits_per_worker_per_window:
            raise DepositRejected(
                f"{worker_id} exceeded {self.envelope.max_deposits_per_worker_per_window} "
                f"deposits per {window:.0f}s window"
            )
        log.append(now)

    # -- dynamics -----------------------------------------------------------

    def step_dynamics(self) -> None:
        """One evaporation + diffusion tick."""
        keep = 1.0 - self.decay_rate
        for cell in self.nodes.values():
            for c in FIELD_CHANNELS:
                cell[c] *= keep
        for cell in self.edges.values():
            for c in FIELD_CHANNELS:
                cell[c] *= keep

        if self.diffusion_rate <= 0:
            return
        deltas: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for node, cell in self.nodes.items():
            neigh = [n for n in self._neighbours.get(node, []) if n in self.nodes]
            if not neigh:
                continue
            for c in self.diffusing_channels:
                out = cell[c] * self.diffusion_rate
                if out <= 0:
                    continue
                share = out / len(neigh)
                deltas[node][c] -= out
                for n in neigh:
                    deltas[n][c] += share
        for node, chans in deltas.items():
            for c, d in chans.items():
                self.nodes[node][c] = _clamp(self.nodes[node][c] + d)

    # -- read path ----------------------------------------------------------

    def query(self, region: list[str] | None = None) -> FieldSnapshot:
        nodes = {
            n: dict(v)
            for n, v in self.nodes.items()
            if region is None or n in region
        }
        edges = {
            e: dict(v)
            for e, v in self.edges.items()
            if region is None or e[0] in region or e[1] in region
        }
        return FieldSnapshot(
            swarm_mission_id=self.swarm_mission_id, nodes=nodes, edges=edges
        )

    def close(self) -> None:
        self.closed = True

    # -- guardrail probes (consumed by has.guardrails) ----------------------

    def max_channel_value(self, channel: str) -> float:
        vals = [c[channel] for c in self.nodes.values()]
        vals += [c[channel] for c in self.edges.values()]
        return max(vals) if vals else 0.0

    def channel_entropy(self, channel: str) -> float:
        """Normalized Shannon entropy of a channel over edges.

        Near 0 => all signal piled on one option (premature-convergence
        smell); near 1 => uniform.
        """
        vals = [c[channel] for c in self.edges.values()]
        total = sum(vals)
        if total <= 0 or len(vals) < 2:
            return 1.0
        probs = [v / total for v in vals if v > 0]
        h = -sum(p * math.log(p) for p in probs)
        return h / math.log(len(vals))

    def _cell(self, kind: str, target: tuple) -> dict[str, float]:
        if kind == "node":
            key = target[0]
            if key not in self.nodes:
                raise DepositRejected(f"unknown node {key!r}")
            return self.nodes[key]
        if kind == "edge":
            key = (target[0], target[1])
            if key not in self.edges:
                raise DepositRejected(f"unknown edge {key!r}")
            return self.edges[key]
        raise DepositRejected(f"unknown target kind {kind!r}")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
