"""Layer 3 boundary — the Swarm Mission Broker and the swarm run loop.

The broker turns a TaskGraph fragment into a bounded swarm problem, spins
up a worker cohort inside the policy envelope, runs the decentralized
adaptation loop (sense -> decide -> act -> deposit, with evaporation and
diffusion between rounds), and packages the result as a
``SwarmRecommendation``. The swarm proposes; it never executes.

Termination: max steps, reward plateau, or circuit-breaker trip.
"""

from __future__ import annotations

import random
import statistics
from collections import deque

from .contracts import (
    PolicyEnvelope,
    RewardProfile,
    SwarmMission,
    SwarmRecommendation,
    TaskNode,
)
from .guardrails import BreakerConfig, CircuitBreaker, InvariantMonitor
from .reward import RewardEngine
from .substrate import PheromoneField
from .telemetry import Telemetry
from .workers import (
    AssignmentProblem,
    Candidate,
    RouterWorker,
    ScoutWorker,
    SentinelWorker,
    TunerWorker,
)


class SwarmMissionBroker:
    def __init__(self, telemetry: Telemetry, seed: int | None = None) -> None:
        self.telemetry = telemetry
        self.rng = random.Random(seed)

    def create_mission(self, mission_id: str, task: TaskNode,
                       objective_vector: dict[str, float],
                       envelope: PolicyEnvelope,
                       max_steps: int = 400) -> SwarmMission:
        mission = SwarmMission(
            mission_id=mission_id,
            origin_task_id=task.task_id,
            objective_type=task.type,
            objective_vector=objective_vector,
            discrete_actions=["assign_crew_to_zone", "hold_for_more_info"],
            continuous_params=["priority_weight", "reroute_threshold"],
            field_channels=["intent", "success", "risk", "congestion",
                            "trust", "novelty", "claimed", "evidence"],
            worker_types=["scout", "router", "tuner", "sentinel"],
            reward_profile_id=task.reward_profile_id or "reward_default_v2",
            max_steps=max_steps,
            policy_envelope=envelope,
        )
        self.telemetry.emit(
            "swarm.mission.created",
            swarm_mission_id=mission.swarm_mission_id,
            origin_task_id=task.task_id,
            field_namespace=(
                f"field://{mission_id}/{mission.swarm_mission_id}"
            ),
        )
        return mission

    def run(self, mission: SwarmMission, problem: AssignmentProblem,
            reward_profile: RewardProfile,
            n_scouts: int = 3, n_routers: int = 6,
            breaker_config: BreakerConfig | None = None,
            ) -> tuple[SwarmRecommendation, CircuitBreaker]:
        pheromone_field = PheromoneField(
            swarm_mission_id=mission.swarm_mission_id,
            nodes=list(problem.sites) + list(problem.entities),
            edges=[(e, s) for e in problem.entities for s in problem.sites],
            envelope=mission.policy_envelope,
            telemetry=self.telemetry,
        )
        reward_engine = RewardEngine(reward_profile, self.telemetry)
        breaker = CircuitBreaker(self.telemetry,
                                 breaker_config or BreakerConfig())
        monitor = InvariantMonitor(pheromone_field, self.telemetry, breaker)

        scouts = [ScoutWorker(pheromone_field, problem, self.rng)
                  for _ in range(n_scouts)]
        routers = [RouterWorker(pheromone_field, problem, self.rng, reward_engine)
                   for _ in range(n_routers)]
        tuner = TunerWorker(
            pheromone_field, problem, self.rng, reward_engine,
            param_bounds={"priority_weight": (0.2, 3.0),
                          "reroute_threshold": (0.0, 1.0)},
        )
        sentinel = SentinelWorker(pheromone_field, problem, self.rng)

        best: Candidate | None = None
        reward_window: deque[float] = deque(maxlen=mission.plateau_window)
        params = {"priority_weight": 1.0, "reroute_threshold": 0.5}
        steps_run = 0

        for step in range(1, mission.max_steps + 1):
            steps_run = step
            sentinel.step()
            for scout in scouts:
                scout.step()
            if step % 5 == 0:
                params = tuner.step()

            round_best: Candidate | None = None
            for router in routers:
                candidate = router.step(params)
                reward_window.append(candidate.reward)
                if round_best is None or candidate.reward > round_best.reward:
                    round_best = candidate
            if round_best and (best is None or round_best.reward > best.reward):
                best = round_best

            pheromone_field.step_dynamics()
            monitor.check(step, list(reward_window))
            if breaker.open:
                break

            if (len(reward_window) == mission.plateau_window
                    and statistics.pstdev(reward_window) < mission.reward_plateau_eps
                    and best is not None):
                self.telemetry.emit("swarm.terminated",
                                    swarm_mission_id=mission.swarm_mission_id,
                                    reason="reward plateau", step=step)
                break

        recommendation = self._package(mission, problem, reward_engine,
                                        pheromone_field, best, steps_run)
        pheromone_field.close()
        return recommendation, breaker

    def _package(self, mission: SwarmMission, problem: AssignmentProblem,
                 reward_engine: RewardEngine, pheromone_field: PheromoneField,
                 best: Candidate | None, steps_run: int) -> SwarmRecommendation:
        if best is None:
            return SwarmRecommendation(
                mission_id=mission.mission_id,
                swarm_mission_id=mission.swarm_mission_id,
                recommended_action={"type": "hold_for_more_info"},
                supporting_actions=[], expected_reward=0.0, confidence=0.0,
                risk_score=1.0, requires_approval=True,
                explanation={"top_factors": ["no feasible candidate found"]},
            )

        # Difference rewards: each entity's marginal contribution to the
        # collective outcome ("how much worse without me?").
        full = reward_engine.evaluate("collective", 
                                      problem.outcome_features(best.assignment))
        contributions = {}
        for entity in list(best.assignment):
            without = {k: v for k, v in best.assignment.items() if k != entity}
            partial = reward_engine.evaluate(
                "collective", problem.outcome_features(without))
            contributions[entity] = round(full.reward - partial.reward, 4)

        snapshot = pheromone_field.query()
        confidence = min(0.95, 0.5 + 0.5 * max(0.0, min(1.0, best.reward)))
        top_edges = sorted(
            ((e, c["success"]) for e, c in snapshot.edges.items()),
            key=lambda kv: kv[1], reverse=True)[:3]

        return SwarmRecommendation(
            mission_id=mission.mission_id,
            swarm_mission_id=mission.swarm_mission_id,
            recommended_action={
                "type": "dispatch_plan",
                "assignment": dict(best.assignment),
            },
            supporting_actions=[
                {"type": "assign_crew_to_zone",
                 "params": {"crew_id": e, "zone_id": s}}
                for e, s in best.assignment.items()
            ],
            expected_reward=round(best.reward, 4),
            confidence=round(confidence, 3),
            risk_score=round(best.features.risk, 3),
            requires_approval=True,
            explanation={
                "steps_run": steps_run,
                "field_snapshot_id": snapshot.field_snapshot_id,
                "marginal_contributions": contributions,
                "top_factors": [
                    f"high success pheromone on {a}->{b} ({v:.2f})"
                    for (a, b), v in top_edges
                ],
            },
        )
