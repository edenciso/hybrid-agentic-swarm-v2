"""Smoke tests for the HAS v2 reference implementation.

Run with:  python -m pytest tests/ -q     (or plain `python tests/test_smoke.py`)
"""

from __future__ import annotations

import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from has import (
    ActionClass,
    ActionRequest,
    ApprovalService,
    BreakerConfig,
    CircuitBreaker,
    DepositRejected,
    FieldUpdate,
    GovernedMemory,
    InvariantMonitor,
    MemoryWrite,
    PheromoneField,
    PolicyEngine,
    PolicyEnvelope,
    RewardProfile,
    SwarmMissionBroker,
    TaskNode,
    Telemetry,
)
from has.reward import OutcomeFeatures, RewardEngine


def make_field(tmp: Path, **envelope_kwargs) -> tuple[PheromoneField, Telemetry]:
    telemetry = Telemetry("mis_test", out_dir=tmp)
    envelope = PolicyEnvelope(**envelope_kwargs)
    f = PheromoneField(
        "swm_test", nodes=["z1", "z2"], edges=[("c1", "z1"), ("c1", "z2")],
        envelope=envelope, telemetry=telemetry)
    return f, telemetry


def _update(worker="router_x", update_type="deposit", kind="node",
            target=("z1",), deltas=None):
    return FieldUpdate(
        swarm_mission_id="swm_test", worker_id=worker,
        update_type=update_type, target_kind=kind, target=target,
        channel_deltas=deltas or {"success": 0.1})


# -- substrate invariants (poisoning defenses) ------------------------------

def test_delta_bound_rejected(tmp_path):
    f, _ = make_field(tmp_path, max_field_delta_per_update=0.25)
    with pytest.raises(DepositRejected):
        f.apply(_update(deltas={"success": 0.5}))


def test_non_sentinel_cannot_reduce_risk(tmp_path):
    f, _ = make_field(tmp_path)
    f.apply(_update(worker="sentinel_1", deltas={"risk": 0.2}),
            worker_role="sentinel")
    with pytest.raises(DepositRejected):
        f.apply(_update(worker="router_1", update_type="inhibit",
                        deltas={"risk": 0.1}), worker_role="router")
    # sentinels may reduce risk
    f.apply(_update(worker="sentinel_1", update_type="inhibit",
                    deltas={"risk": 0.1}), worker_role="sentinel")
    assert f.nodes["z1"]["risk"] < 0.2


def test_deposit_rate_limit(tmp_path):
    f, _ = make_field(tmp_path, max_deposits_per_worker_per_window=3)
    for _ in range(3):
        f.apply(_update())
    with pytest.raises(DepositRejected):
        f.apply(_update())


def test_saturate_guard_and_decay(tmp_path):
    f, _ = make_field(tmp_path, max_deposits_per_worker_per_window=10_000)
    for _ in range(500):
        f.apply(_update(deltas={"success": 0.2}))
        f.step_dynamics()
    assert f.nodes["z1"]["success"] < 0.95  # asymptote, never pinned


def test_closed_namespace_rejects(tmp_path):
    f, _ = make_field(tmp_path)
    f.close()
    with pytest.raises(DepositRejected):
        f.apply(_update())


# -- reward engine -----------------------------------------------------------

def test_hard_penalty_invalidates(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = RewardEngine(RewardProfile("r1"), telemetry)
    result = engine.evaluate("w1", OutcomeFeatures(
        value_gain=1.0, violations=["unsafe_action"]))
    assert result.invalidated and result.reward < -100


def test_difference_reward_term(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = RewardEngine(RewardProfile("r1", difference_weight=1.0), telemetry)
    with_d = engine.evaluate("w1", OutcomeFeatures(value_gain=0.5),
                             collective_best_without=0.2,
                             collective_best_with=0.5)
    without_d = engine.evaluate("w1", OutcomeFeatures(value_gain=0.5))
    assert with_d.reward == pytest.approx(without_d.reward + 0.3)


# -- policy engine -----------------------------------------------------------

def _request(principal="tool_exec_01", action_class=ActionClass.RECOMMEND,
             risk=0.1, confidence=0.9):
    return ActionRequest(
        mission_id="mis_test", principal_id=principal,
        parent_agent_id="master_01", action_class=action_class,
        tool_name="dispatch.publish", params={}, resource_targets=[],
        risk_score=risk, confidence=confidence)


def test_workers_limited_to_class_1(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = PolicyEngine(PolicyEnvelope(), telemetry)
    assert engine.decide(_request(principal="router_44",
                                  action_class=ActionClass.SIMULATE)
                         ).decision == "deny"
    assert engine.decide(_request(principal="router_44",
                                  action_class=ActionClass.RECOMMEND)
                         ).decision == "allow"


def test_class_5_requires_approval(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = PolicyEngine(PolicyEnvelope(no_real_world_execution=False),
                          telemetry, autonomy_profile="bounded_auto")
    decision = engine.decide(_request(action_class=ActionClass.IRREVERSIBLE))
    assert decision.decision == "require_approval"
    assert "human_approval" in decision.obligations


def test_risk_over_envelope_denied(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = PolicyEngine(PolicyEnvelope(max_risk_score=0.3,
                                         no_real_world_execution=False),
                          telemetry, autonomy_profile="bounded_auto")
    decision = engine.decide(_request(
        action_class=ActionClass.BOUNDED_REAL_WORLD, risk=0.6))
    assert decision.decision == "deny"


def test_recommend_only_profile_blocks_class_4(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    engine = PolicyEngine(PolicyEnvelope(no_real_world_execution=False),
                          telemetry, autonomy_profile="recommend_only")
    decision = engine.decide(_request(action_class=ActionClass.BOUNDED_REAL_WORLD))
    assert decision.decision == "deny"


# -- guardrails ---------------------------------------------------------------

def test_breaker_trips_on_saturation(tmp_path):
    f, telemetry = make_field(tmp_path, max_deposits_per_worker_per_window=10_000)
    breaker = CircuitBreaker(telemetry, BreakerConfig(max_saturation=0.5))
    monitor = InvariantMonitor(f, telemetry, breaker)
    for _ in range(10):
        f.apply(_update(deltas={"success": 0.2}))
    monitor.check(step=1, recent_rewards=[0.1])
    assert breaker.open and "saturation" in breaker.reason


def test_breaker_trips_on_reward_collapse(tmp_path):
    f, telemetry = make_field(tmp_path)
    breaker = CircuitBreaker(telemetry, BreakerConfig())
    monitor = InvariantMonitor(f, telemetry, breaker)
    monitor.check(step=1, recent_rewards=[-10.0, -12.0])
    assert breaker.open and breaker.reason == "reward collapse"


# -- governed memory ----------------------------------------------------------

def test_curated_memory_requires_provenance(tmp_path):
    telemetry = Telemetry("mis_test", out_dir=tmp_path)
    memory = GovernedMemory(telemetry, root=tmp_path)
    with pytest.raises(ValueError):
        memory.write(MemoryWrite(
            mission_id="mis_test", agent_id="curator", memory_scope="semantic",
            title="t", content="c"))
    ref = memory.write(MemoryWrite(
        mission_id="mis_test", agent_id="curator", memory_scope="semantic",
        title="Lesson", content="c", derived_from=["act_1"]))
    assert ref.startswith("mem://")


# -- end-to-end mini run --------------------------------------------------------

@dataclass
class TinyProblem:
    entities: list[str] = field(default_factory=lambda: ["c1", "c2"])
    sites: list[str] = field(default_factory=lambda: ["z1", "z2", "z3"])

    def heuristic(self, e, s, params):
        return {"z1": 1.0, "z2": 0.6, "z3": 0.2}[s]

    def outcome_features(self, assignment):
        if not assignment:
            return OutcomeFeatures()
        value = sum({"z1": 1.0, "z2": 0.6, "z3": 0.2}[z]
                    for z in assignment.values()) / 2
        spread = len(set(assignment.values())) / len(assignment)
        return OutcomeFeatures(value_gain=value * spread, cost=0.2,
                               risk=0.1, team_synergy=spread)

    def hazard_level(self, s):
        return 0.1

    def observe_evidence(self, e, s):
        return 0.8


def test_end_to_end_recommendation(tmp_path):
    telemetry = Telemetry("mis_e2e", out_dir=tmp_path)
    broker = SwarmMissionBroker(telemetry, seed=1)
    task = TaskNode("t1", "graph_assignment", "assign", ActionClass.SIMULATE,
                    reward_profile_id="r1")
    mission = broker.create_mission(
        "mis_e2e", task, {"throughput": 1.0},
        PolicyEnvelope(max_deposits_per_worker_per_window=100_000),
        max_steps=60)
    rec, breaker = broker.run(mission, TinyProblem(),
                              RewardProfile("r1"), n_scouts=1, n_routers=3)
    assert not breaker.open
    assert rec.expected_reward > 0
    assert rec.requires_approval
    assert set(rec.recommended_action["assignment"]) == {"c1", "c2"}
    assert telemetry.count("field.update") > 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
