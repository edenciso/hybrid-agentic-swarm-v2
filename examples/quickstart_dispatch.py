"""HAS v2 quickstart — storm restoration dispatch.

The running example from the v1 interface contracts, end to end:

    4 repair crews must be assigned to 8 storm-damaged zones. Zones differ
    in outage severity, travel time, and hazard level. The swarm searches
    the assignment space; the deterministic plane decides what may become
    real.

What happens, plane by plane:

  Layer 1  Mission intake -> master orchestrator (Hermes adapter) plans a
           task graph and spawns a governed cohort.
  Layer 2  A multi-channel pheromone field is initialized over the
           crew-zone graph (deposit / evaporate / diffuse).
  Layer 3  Scouts raise evidence, sentinels write the risk channel,
           a PSO tuner shapes the heuristic, ACO routers converge on an
           assignment; the reward engine scores with difference rewards.
  Layer 4  The recommendation (class 4, bounded real-world) hits the
           policy engine -> requires human approval -> the operator
           approves -> the tool broker executes -> outcome persists to
           governed memory; every step lands in runs/<mission>.jsonl.

Run it:

    python examples/quickstart_dispatch.py

No dependencies beyond the Python standard library.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from has import (  # noqa: E402
    ApprovalService,
    GovernedMemory,
    HermesAdapter,
    MasterOrchestrator,
    MissionEnvelope,
    PolicyEngine,
    PolicyEnvelope,
    RewardProfile,
    SwarmMissionBroker,
    Telemetry,
    ToolBroker,
)
from has.reward import OutcomeFeatures  # noqa: E402

SEED = 7
rng = random.Random(SEED)


# ---------------------------------------------------------------------------
# Domain adapter: the world the swarm searches (AssignmentProblem protocol)
# ---------------------------------------------------------------------------


@dataclass
class DispatchProblem:
    entities: list[str] = field(default_factory=lambda: [f"crew_{i}" for i in range(1, 5)])
    sites: list[str] = field(default_factory=lambda: [f"zone_{i:02d}" for i in range(1, 9)])

    def __post_init__(self) -> None:
        self.severity = {s: rng.uniform(0.2, 1.0) for s in self.sites}       # outage size
        self.hazard = {s: rng.uniform(0.0, 0.6) for s in self.sites}         # downed lines etc.
        self.travel = {(e, s): rng.uniform(0.2, 1.0)                         # normalized hours
                       for e in self.entities for s in self.sites}
        self.skill_fit = {(e, s): rng.uniform(0.5, 1.0)
                          for e in self.entities for s in self.sites}

    # -- AssignmentProblem protocol ----------------------------------------

    def heuristic(self, entity: str, site: str, params: dict[str, float]) -> float:
        w = params.get("priority_weight", 1.0)
        return (self.severity[site] ** w) * self.skill_fit[(entity, site)] \
            / (0.1 + self.travel[(entity, site)])

    def outcome_features(self, assignment: dict[str, str]) -> OutcomeFeatures:
        if not assignment:
            return OutcomeFeatures()
        covered = set(assignment.values())
        value = sum(self.severity[z] * self.skill_fit[(c, z)]
                    for c, z in assignment.items()) / len(self.entities)
        coverage_bonus = len(covered) / len(assignment)          # spread out
        cost = sum(self.travel[(c, z)] for c, z in assignment.items()) / len(assignment)
        congestion = 1.0 - coverage_bonus                        # crews piling up
        risk = max(self.hazard[z] for z in covered)
        return OutcomeFeatures(
            value_gain=min(1.0, value * coverage_bonus),
            cost=cost,
            congestion=congestion,
            risk=risk,
            novelty=0.0,
            team_synergy=coverage_bonus,
            claimed_level=0.0,
        )

    def hazard_level(self, site: str) -> float:
        return self.hazard[site]

    def observe_evidence(self, entity: str, site: str) -> float:
        return self.skill_fit[(entity, site)]


# ---------------------------------------------------------------------------
# The human in the loop (delivered via the Hermes messaging gateway in prod)
# ---------------------------------------------------------------------------


def ops_manager(action, decision) -> bool:
    print(f"\n  [APPROVAL GATE] {decision.reason}")
    print(f"    action     : {action.tool_name} (class {int(action.action_class)})")
    print(f"    risk score : {action.risk_score:.2f}   "
          f"confidence: {action.confidence:.2f}")
    print("    ops_manager approves ✔")
    return True


def main() -> None:
    print("=" * 66)
    print("HAS v2 quickstart — storm restoration dispatch")
    print("=" * 66)

    # ---- Layer 1: mission intake + deterministic planning ----------------
    envelope = MissionEnvelope(
        tenant_id="acme-grid",
        objective="Reduce restoration backlog within safety and budget limits",
        objective_vector={"throughput": 0.45, "cost_efficiency": 0.20,
                          "safety": 0.25, "latency": 0.10},
        hard_constraints=["no irreversible actuator changes without approval",
                          "crew certifications must match job requirements"],
        autonomy_profile="bounded_auto",
        currency_budget_usd=10_000,
    )
    telemetry = Telemetry(envelope.mission_id)
    hermes = HermesAdapter(telemetry)
    memory = GovernedMemory(telemetry)
    master = MasterOrchestrator(envelope, telemetry, hermes, memory)

    print(f"\nmission {envelope.mission_id}")
    print(f"hermes runtime detected: {hermes.available()} "
          f"(falling back to deterministic planner if False)")

    task_graph = master.plan()
    master.spawn_cohort()
    print(f"planned {len(task_graph.tasks)} tasks, "
          f"spawned {len(master.agents)} governed agents")

    # ---- Layers 2+3: bounded swarm search ---------------------------------
    problem = DispatchProblem()
    # The per-worker deposit rate limit is a wall-clock defense for
    # distributed deployments; an in-process simulation compresses hours
    # into milliseconds, so size the window for simulated time here.
    swarm_envelope = PolicyEnvelope(max_risk_score=0.75,
                                    no_real_world_execution=False,
                                    max_deposits_per_worker_per_window=100_000)
    broker = SwarmMissionBroker(telemetry, seed=SEED)
    swarm_mission = broker.create_mission(
        envelope.mission_id, task_graph.tasks[0],
        envelope.objective_vector, swarm_envelope, max_steps=250)

    print(f"\nswarm mission {swarm_mission.swarm_mission_id}: "
          f"{len(problem.entities)} crews x {len(problem.sites)} zones")
    recommendation, breaker = broker.run(
        swarm_mission, problem,
        RewardProfile(reward_profile_id="reward_dispatch_v2"))

    print(f"circuit breaker: {breaker.status}"
          + (f" ({breaker.reason})" if breaker.open else ""))
    print(f"\nswarm recommendation {recommendation.recommendation_id}")
    print(f"  expected reward : {recommendation.expected_reward}")
    print(f"  confidence      : {recommendation.confidence}")
    print(f"  risk score      : {recommendation.risk_score}")
    for a in recommendation.supporting_actions:
        p = a["params"]
        print(f"    {p['crew_id']} -> {p['zone_id']}")
    print("  marginal contributions (difference rewards):")
    for crew, d in recommendation.explanation["marginal_contributions"].items():
        print(f"    {crew}: {d:+.4f}")

    # ---- Layer 4: policy gate -> approval -> execution -> memory ---------
    policy = PolicyEngine(swarm_envelope, telemetry,
                          autonomy_profile=envelope.autonomy_profile)
    approvals = ApprovalService(telemetry, approver=ops_manager)
    tool_broker = ToolBroker(policy, approvals, telemetry)
    tool_broker.register(
        "dispatch.publish",
        lambda params: {"external_ref": "dispatch-job-91921",
                        "queued_jobs": len(params["plan"]["assignment"])})

    result = master.act_on(recommendation, tool_broker)
    print(f"\nexecution: {result.status}  {result.tool_result}")

    memory_ref = master.close_out(recommendation, result)
    print(f"memory written: {memory_ref}")

    print(f"\ntelemetry ({sum(telemetry.summary().values())} events "
          f"-> {telemetry.path}):")
    print(json.dumps(telemetry.summary(), indent=2))


if __name__ == "__main__":
    main()
