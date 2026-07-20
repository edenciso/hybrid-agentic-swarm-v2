"""Layer 3 — swarm-intelligence workers.

Workers are deliberately *lightweight*: cheap policies running a tight
loop — sense the local field, decide, act, deposit, update. They never
call production tools; they emit only class-0 (observe) and class-1
(recommend) outputs. On the Hermes side, a worker cohort maps to isolated
subagents with a cheap ``model_override`` (or, as here, no LLM at all),
escalating to a heavy reasoner only for genuinely ambiguous decisions.

Archetypes (v1 §3.3, unchanged in v2):

* **Scout**    — uncertainty reduction; boosts ``novelty``/``evidence``
* **Router**   — discrete assignment via Ant Colony Optimization
* **Tuner**    — continuous parameter search via Particle Swarm Optimization
* **Sentinel** — risk sensing; the only role allowed to *lower* ``risk``
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import FieldUpdate, new_id
from .reward import OutcomeFeatures, RewardEngine
from .substrate import DepositRejected, PheromoneField


class AssignmentProblem(Protocol):
    """Domain adapter a concrete use case must implement (see examples/)."""

    entities: list[str]                       # e.g. crews
    sites: list[str]                          # e.g. zones

    def heuristic(self, entity: str, site: str, params: dict[str, float]) -> float:
        """Static desirability of entity->site, > 0."""
        ...

    def outcome_features(self, assignment: dict[str, str]) -> OutcomeFeatures:
        """Measured/simulated features for a full assignment."""
        ...

    def hazard_level(self, site: str) -> float:
        """Ground-truth risk in [0, 1] a sentinel can observe."""
        ...

    def observe_evidence(self, entity: str, site: str) -> float:
        """Ground-truth evidence strength in [0, 1] a scout can gather."""
        ...


@dataclass
class Candidate:
    assignment: dict[str, str]
    reward: float
    features: OutcomeFeatures
    worker_id: str


class BaseWorker:
    role = "worker"

    def __init__(self, field_ref: PheromoneField, problem: AssignmentProblem,
                 rng: random.Random) -> None:
        self.worker_id = new_id(self.role)
        self.field = field_ref
        self.problem = problem
        self.rng = rng

    def _deposit(self, update_type: str, target_kind: str, target: tuple,
                 deltas: dict[str, float], causation_id: str | None = None) -> None:
        try:
            self.field.apply(
                FieldUpdate(
                    swarm_mission_id=self.field.swarm_mission_id,
                    worker_id=self.worker_id,
                    update_type=update_type,
                    target_kind=target_kind,
                    target=target,
                    channel_deltas=deltas,
                    causation_id=causation_id,
                ),
                worker_role=self.role,
            )
        except DepositRejected:
            # Rejected deposits are dropped; the substrate already emitted
            # nothing and the guardrail layer sees the rejection count.
            self.field.telemetry.emit(
                "field.update.rejected", worker_id=self.worker_id
            )


class ScoutWorker(BaseWorker):
    """Explores under-sampled edges; raises novelty and evidence."""

    role = "scout"

    def step(self) -> None:
        entity = self.rng.choice(self.problem.entities)
        site = self.rng.choice(self.problem.sites)
        snapshot = self.field.query()
        cell = snapshot.edges.get((entity, site), {})
        if cell.get("evidence", 0.0) < 0.5:
            strength = self.problem.observe_evidence(entity, site)
            self._deposit(
                "deposit", "edge", (entity, site),
                {"evidence": 0.15 * strength, "novelty": 0.10},
            )


class RouterWorker(BaseWorker):
    """ACO: builds a full assignment probabilistically from pheromone x
    heuristic, evaluates it, reinforces the edges it used in proportion to
    the reward, and inhibits on failure."""

    role = "router"

    def __init__(self, field_ref, problem, rng, reward_engine: RewardEngine,
                 alpha: float = 1.0, beta: float = 2.0) -> None:
        super().__init__(field_ref, problem, rng)
        self.reward_engine = reward_engine
        self.alpha = alpha   # pheromone influence
        self.beta = beta     # heuristic influence

    def step(self, params: dict[str, float]) -> Candidate:
        snapshot = self.field.query()
        assignment: dict[str, str] = {}
        for entity in self.problem.entities:
            weights = []
            for site in self.problem.sites:
                cell = snapshot.edges.get((entity, site), {})
                tau = 0.05 + cell.get("success", 0.0)          # pheromone
                eta = max(1e-6, self.problem.heuristic(entity, site, params))
                penalty = 1.0 + 2.0 * cell.get("risk", 0.0) \
                              + 1.0 * cell.get("congestion", 0.0) \
                              + 1.0 * cell.get("claimed", 0.0)
                weights.append((tau ** self.alpha) * (eta ** self.beta) / penalty)
            total = sum(weights)
            probs = [w / total for w in weights]
            site = self.rng.choices(self.problem.sites, weights=probs, k=1)[0]
            assignment[entity] = site

        features = self.problem.outcome_features(assignment)
        result = self.reward_engine.evaluate(self.worker_id, features)

        act_id = new_id("prop")
        if result.invalidated or result.reward <= 0:
            for entity, site in assignment.items():
                self._deposit("inhibit", "edge", (entity, site),
                              {"success": 0.02}, causation_id=act_id)
        else:
            gain = min(0.12, 0.15 * result.reward)
            for entity, site in assignment.items():
                self._deposit("reinforce", "edge", (entity, site),
                              {"success": gain, "claimed": 0.03},
                              causation_id=act_id)

        return Candidate(assignment=assignment, reward=result.reward,
                         features=features, worker_id=self.worker_id)


@dataclass
class _Particle:
    position: dict[str, float]
    velocity: dict[str, float]
    best_position: dict[str, float]
    best_score: float = float("-inf")


class TunerWorker(BaseWorker):
    """PSO over the continuous parameters that shape the router heuristic
    (e.g. priority_weight, reroute_threshold). Particles are pulled toward
    their own best-found and the swarm's best-found position."""

    role = "tuner"

    def __init__(self, field_ref, problem, rng, reward_engine: RewardEngine,
                 param_bounds: dict[str, tuple[float, float]],
                 n_particles: int = 6, inertia: float = 0.6,
                 c_personal: float = 1.2, c_swarm: float = 1.2) -> None:
        super().__init__(field_ref, problem, rng)
        self.reward_engine = reward_engine
        self.bounds = param_bounds
        self.inertia = inertia
        self.c_personal = c_personal
        self.c_swarm = c_swarm
        self.global_best: dict[str, float] = {
            k: (lo + hi) / 2 for k, (lo, hi) in param_bounds.items()
        }
        self.global_best_score = float("-inf")
        self.particles = [
            _Particle(
                position={k: rng.uniform(lo, hi) for k, (lo, hi) in param_bounds.items()},
                velocity={k: 0.0 for k in param_bounds},
                best_position={k: rng.uniform(lo, hi) for k, (lo, hi) in param_bounds.items()},
            )
            for _ in range(n_particles)
        ]

    def _score(self, params: dict[str, float]) -> float:
        """Greedy assignment under these params, scored by the reward engine."""
        assignment = {
            e: max(self.problem.sites, key=lambda s: self.problem.heuristic(e, s, params))
            for e in self.problem.entities
        }
        features = self.problem.outcome_features(assignment)
        return self.reward_engine.evaluate(self.worker_id, features).reward

    def step(self) -> dict[str, float]:
        for p in self.particles:
            score = self._score(p.position)
            if score > p.best_score:
                p.best_score, p.best_position = score, dict(p.position)
            if score > self.global_best_score:
                self.global_best_score, self.global_best = score, dict(p.position)
        for p in self.particles:
            for k, (lo, hi) in self.bounds.items():
                r1, r2 = self.rng.random(), self.rng.random()
                p.velocity[k] = (
                    self.inertia * p.velocity[k]
                    + self.c_personal * r1 * (p.best_position[k] - p.position[k])
                    + self.c_swarm * r2 * (self.global_best[k] - p.position[k])
                )
                p.position[k] = min(hi, max(lo, p.position[k] + p.velocity[k]))
        return dict(self.global_best)


class SentinelWorker(BaseWorker):
    """Monitors hazard ground truth and writes the risk channel. Sentinels
    are the only role permitted to *reduce* risk (substrate invariant)."""

    role = "sentinel"

    def step(self) -> None:
        for site in self.problem.sites:
            hazard = self.problem.hazard_level(site)
            snapshot = self.field.query(region=[site])
            current = snapshot.nodes[site]["risk"]
            gap = hazard - current
            if abs(gap) > 0.05:
                delta = max(-0.2, min(0.2, gap * 0.5))
                if delta >= 0:
                    self._deposit("deposit", "node", (site,), {"risk": delta})
                else:
                    self._deposit("inhibit", "node", (site,), {"risk": -delta})
