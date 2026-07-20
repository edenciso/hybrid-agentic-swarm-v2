"""Layer 3 — the reward engine.

v2 keeps v1's factorized, constraint-aware canonical reward

    R_t = a*V_t - b*C_t - g*K_t - d*S_t + e*N_t + z*T_t

and adds the two pieces doc-2 argued naive designs miss:

* a **difference-reward** term ``D_t`` — the worker's marginal contribution
  ("how much worse would the collective have done without me?"), which
  aligns selfish local learning with the global objective and tames the
  credit-assignment problem, and
* a **redundancy penalty** driven by the substrate's ``claimed`` channel,
  which discourages piling onto work another worker already claimed.

Hard-constraint violations produce large negative rewards, invalidate the
candidate, and surface to the orchestration layer via telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import RewardProfile
from .telemetry import Telemetry


@dataclass
class OutcomeFeatures:
    """Measured / simulated features of a candidate action's outcome."""

    value_gain: float = 0.0          # V_t  mission value created (0..1)
    cost: float = 0.0                # C_t  operational cost (0..1)
    congestion: float = 0.0          # K_t  contention created (0..1)
    risk: float = 0.0                # S_t  safety/compliance exposure (0..1)
    novelty: float = 0.0             # N_t  exploration gain (0..1)
    team_synergy: float = 0.0        # T_t  cooperation bonus (0..1)
    claimed_level: float = 0.0       # substrate 'claimed' at the target
    violations: list[str] = field(default_factory=list)


@dataclass
class RewardResult:
    reward: float
    components: dict[str, float]
    penalties_applied: list[str]
    invalidated: bool


class RewardEngine:
    def __init__(self, profile: RewardProfile, telemetry: Telemetry) -> None:
        self.profile = profile
        self.telemetry = telemetry

    def evaluate(
        self,
        worker_id: str,
        features: OutcomeFeatures,
        collective_best_without: float | None = None,
        collective_best_with: float | None = None,
        emit: bool = False,
    ) -> RewardResult:
        p = self.profile
        components = {
            "value": p.value_weight * features.value_gain,
            "cost": -p.cost_weight * features.cost,
            "congestion": -p.congestion_weight * features.congestion,
            "safety": -p.safety_weight * features.risk,
            "novelty": p.novelty_weight * features.novelty,
            "team": p.team_weight * features.team_synergy,
            "redundancy": -p.redundancy_penalty * features.claimed_level,
        }

        # difference reward: marginal contribution to the collective outcome
        if collective_best_with is not None and collective_best_without is not None:
            components["difference"] = p.difference_weight * (
                collective_best_with - collective_best_without
            )

        reward = sum(components.values())

        penalties: list[str] = []
        invalidated = False
        for violation in features.violations:
            penalty = self.profile.hard_penalties.get(violation)
            if penalty is not None:
                reward += penalty
                penalties.append(violation)
                invalidated = True

        result = RewardResult(
            reward=round(reward, 6),
            components={k: round(v, 6) for k, v in components.items()},
            penalties_applied=penalties,
            invalidated=invalidated,
        )
        if emit or penalties:
            self.telemetry.emit(
                "reward.evaluate",
                worker_id=worker_id,
                reward=result.reward,
                penalties=penalties,
                invalidated=invalidated,
            )
        return result
