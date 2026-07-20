"""Layer 4 — guardrails: invariant monitors and circuit breakers.

Because swarm behavior is emergent, it cannot be certified by inspecting
code; it is certified by monitoring invariants at runtime and retaining
the authority to stop. Four capabilities (doc-2 Layer 4, formalized here):

1. **Invariant monitors** — properties that must always hold (budget not
   exceeded, no forbidden action attempted), checked continuously.
2. **Substrate anomaly detection** — the pheromone field is an attack
   surface: an adversary or malfunctioning worker that can write to it can
   *poison the field* and steer the whole swarm. Deposit-rate limits and
   delta bounds live in the substrate itself; saturation and rejection-rate
   monitoring live here.
3. **Emergent-behavior detection** — premature convergence (entropy
   collapse), oscillation, runaway feedback loops.
4. **Kill authority** — circuit breakers freeze worker action execution
   (sensing may continue), escalate to the master + safety agent, and emit
   an incident trace. The swarm can never revoke this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .substrate import PheromoneField
from .telemetry import Telemetry


@dataclass
class BreakerConfig:
    max_saturation: float = 0.98          # any channel pinned at ceiling
    min_success_entropy: float = 0.05     # premature-convergence floor
    entropy_grace_steps: int = 40         # allow legit convergence early on
    max_rejected_deposits: int = 25       # poisoning / malfunction smell
    max_policy_denials: int = 10          # denial spike
    reward_collapse_threshold: float = -5.0
    step_budget: int | None = None


@dataclass
class CircuitBreaker:
    """Kill authority held by the deterministic plane."""

    telemetry: Telemetry
    config: BreakerConfig = field(default_factory=BreakerConfig)
    status: str = "closed"                # closed | open
    reason: str = ""

    def trip(self, reason: str) -> None:
        if self.status == "open":
            return
        self.status = "open"
        self.reason = reason
        self.telemetry.emit(
            "breaker.state",
            breaker_status="open",
            reason=reason,
            effects=[
                "pause tool execution",
                "allow sensing only",
                "notify master agent",
                "escalate to operator",
            ],
        )

    @property
    def open(self) -> bool:
        return self.status == "open"


class InvariantMonitor:
    """Continuously evaluated checks over the field + telemetry counters."""

    def __init__(self, field_ref: PheromoneField, telemetry: Telemetry,
                 breaker: CircuitBreaker, policy_denials: "callable" = None) -> None:
        self.field = field_ref
        self.telemetry = telemetry
        self.breaker = breaker
        self.policy_denials = policy_denials or (lambda: 0)

    def check(self, step: int, recent_rewards: list[float]) -> None:
        cfg = self.breaker.config

        if cfg.step_budget is not None and step > cfg.step_budget:
            self.breaker.trip("step budget exhausted")
            return

        for channel in ("success", "risk", "congestion"):
            if self.field.max_channel_value(channel) >= cfg.max_saturation:
                self.breaker.trip(f"pheromone saturation on '{channel}' channel")
                return

        if step > cfg.entropy_grace_steps:
            entropy = self.field.channel_entropy("success")
            if entropy < cfg.min_success_entropy:
                self.breaker.trip(
                    f"anomalous convergence: success-channel entropy "
                    f"{entropy:.3f} below floor {cfg.min_success_entropy}"
                )
                return

        if self.telemetry.count("field.update.rejected") > cfg.max_rejected_deposits:
            self.breaker.trip("rejected-deposit spike (possible field poisoning)")
            return

        if self.policy_denials() > cfg.max_policy_denials:
            self.breaker.trip("policy denial spike")
            return

        if recent_rewards and (sum(recent_rewards) / len(recent_rewards)
                               < cfg.reward_collapse_threshold):
            self.breaker.trip("reward collapse")
