"""HAS v2 interface contracts.

Every object crossing a plane boundary in the Hybrid Agentic Swarm is one of
these typed contracts. The design invariants (carried forward from v1 §4.1):

* every request carries ``mission_id``
* every actor has an ``agent_id`` or ``worker_id``
* every real action carries an ``action_class``
* every stateful change carries a ``causation_id``
* every contract object has a ``schema_version``
* every execution request is policy-checkable from structured input

v2 note: the deterministic orchestration plane is Hermes Agent (Nous
Research). Contracts stay runtime-agnostic; only ``runtime_ref`` values and
the spawn contract reference the Hermes runtime (``hermes://...``).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

SCHEMA_VERSION = "2.0"


def new_id(prefix: str) -> str:
    """Short, sortable-enough unique id with a readable prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class ActionClass(IntEnum):
    """Action risk ladder (v1 §5, unchanged in v2).

    Swarm workers may emit classes 0-1 directly. Classes 2-5 must pass
    through the Tool Broker; 4-5 require a policy decision; 5 always
    requires human approval unless exempted by signed policy.
    """

    OBSERVE = 0
    RECOMMEND = 1
    SIMULATE = 2
    REVERSIBLE = 3
    BOUNDED_REAL_WORLD = 4
    IRREVERSIBLE = 5


# --------------------------------------------------------------------------
# Layer 1 — deterministic orchestration (Hermes Agent control plane)
# --------------------------------------------------------------------------


@dataclass
class MissionEnvelope:
    """Normalized mission produced by Mission Intake (POST /v1/missions)."""

    tenant_id: str
    objective: str
    objective_vector: dict[str, float]
    hard_constraints: list[str] = field(default_factory=list)
    soft_constraints: list[str] = field(default_factory=list)
    autonomy_profile: str = "recommend_only"  # recommend_only | bounded_auto
    token_budget: int = 250_000
    currency_budget_usd: float = 0.0
    time_horizon_sec: int = 3600
    max_workers: int = 64          # fork-bomb guard (v2, from doc-2 layer 1)
    max_spawn_depth: int = 2       # fork-bomb guard
    mission_id: str = field(default_factory=lambda: new_id("mis"))
    schema_version: str = SCHEMA_VERSION


@dataclass
class TaskNode:
    task_id: str
    type: str
    description: str
    action_class: ActionClass
    depends_on: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    approval_policy: str | None = None
    reward_profile_id: str | None = None


@dataclass
class TaskGraph:
    mission_id: str
    tasks: list[TaskNode] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def roots(self) -> list[TaskNode]:
        return [t for t in self.tasks if not t.depends_on]


@dataclass
class AgentSpec:
    """Spawn contract for a governed Hermes subagent (POST /v1/agents/spawn).

    Maps onto Hermes Agent primitives:

    * ``profile``          -> a Hermes profile started from *Blank Slate*
                              setup (everything off, opt-in only)
    * ``terminal_backend`` -> Hermes terminal backend used as the sandbox
                              boundary (docker | modal | daytona | ssh |
                              singularity | local)
    * ``toolset_allow``    -> Hermes toolset / MCP tool filter
    * ``secrets_scope``    -> Bitwarden Secrets Manager scope; agents never
                              see raw provider keys
    * ``model_override``   -> Hermes per-task model override (cheap policy
                              models for workers, heavy reasoner on
                              escalation)
    * ``skills_readonly``  -> if True, Hermes autonomous skill creation /
                              self-improvement is disabled for this agent;
                              skill writes must go through the governed
                              memory-write path instead
    """

    agent_id: str
    role: str
    terminal_backend: str = "docker"
    toolset_allow: list[str] = field(default_factory=list)
    toolset_deny: list[str] = field(default_factory=list)
    secrets_scope: str | None = None
    model_override: str | None = None
    skills_readonly: bool = True
    context_scope: list[str] = field(default_factory=lambda: ["mission", "role", "session"])


@dataclass
class SpawnResult:
    agent_id: str
    runtime_ref: str  # e.g. "hermes://profiles/planner_03"
    status: str = "ready"


@dataclass
class ContextPackage:
    """Tiered context (v1 §4.5) delivered to an agent before a run.

    ``constitution`` is the small immutable tier every worker carries
    (objective, invariants, forbidden actions) — doc-2's key addition.
    """

    mission_id: str
    agent_id: str
    constitution: dict[str, Any]
    role_context: dict[str, Any] = field(default_factory=dict)
    session_context: dict[str, Any] = field(default_factory=dict)
    memory_refs: list[str] = field(default_factory=list)
    ttl_sec: int = 900
    schema_version: str = SCHEMA_VERSION


# --------------------------------------------------------------------------
# Layer 2 — stigmergic substrate
# --------------------------------------------------------------------------

#: v2 unified channel set. v1 doc-1 channels plus doc-2's ``claimed`` and
#: ``evidence`` channels. ``success`` subsumes doc-2's "promise"; ``risk``
#: subsumes "danger/taboo".
FIELD_CHANNELS = (
    "intent",       # unsatisfied task demand
    "success",      # recent positive payoff (promise)
    "risk",         # safety / compliance / danger / taboo
    "congestion",   # overused resources
    "cost",         # budget / energy / latency
    "trust",        # reliability of nodes, tools, data sources
    "novelty",      # exploration bonus for underexplored options
    "claimed",      # another worker is already here (anti-redundancy)
    "evidence",     # observed ground truth strength
)


@dataclass
class FieldUpdate:
    """POST /v1/field/update — the only way state enters the substrate."""

    swarm_mission_id: str
    worker_id: str
    update_type: str                       # deposit | reinforce | inhibit
    target_kind: str                       # node | edge
    target: tuple                          # ("zone_12",) or ("crew_8", "zone_12")
    channel_deltas: dict[str, float]
    causation_id: str | None = None
    decay_hint_sec: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    field_update_id: str = field(default_factory=lambda: new_id("fupd"))
    schema_version: str = SCHEMA_VERSION


@dataclass
class FieldSnapshot:
    """POST /v1/field/query response — a worker's local view."""

    swarm_mission_id: str
    nodes: dict[str, dict[str, float]]
    edges: dict[tuple, dict[str, float]]
    timestamp: float = field(default_factory=time.time)
    field_snapshot_id: str = field(default_factory=lambda: new_id("fieldsnap"))
    schema_version: str = SCHEMA_VERSION


# --------------------------------------------------------------------------
# Layer 3 — swarm mission, reward, recommendation
# --------------------------------------------------------------------------


@dataclass
class PolicyEnvelope:
    """The box the swarm is allowed to explore in (doc-2's core principle)."""

    max_risk_score: float = 0.3
    no_real_world_execution: bool = True
    requires_review_if_confidence_lt: float = 0.7
    max_field_delta_per_update: float = 0.25
    max_deposits_per_worker_per_window: int = 30
    deposit_window_sec: float = 10.0


@dataclass
class SwarmMission:
    """POST /v1/swarm/missions — a bounded search problem for the swarm."""

    mission_id: str
    origin_task_id: str
    objective_type: str
    objective_vector: dict[str, float]
    discrete_actions: list[str]
    continuous_params: list[str]
    field_channels: list[str]
    worker_types: list[str]
    reward_profile_id: str
    max_steps: int = 2000
    reward_plateau_eps: float = 0.01
    plateau_window: int = 50
    policy_envelope: PolicyEnvelope = field(default_factory=PolicyEnvelope)
    swarm_mission_id: str = field(default_factory=lambda: new_id("swm"))
    schema_version: str = SCHEMA_VERSION


@dataclass
class RewardProfile:
    """Factorized, constraint-aware reward (v1 §4.9 + doc-2 difference reward).

    R = alpha*V - beta*C - gamma*K - delta*S + eps*N + zeta*T + eta*D
    where D is the difference-reward term (marginal contribution).
    """

    reward_profile_id: str
    value_weight: float = 0.40
    cost_weight: float = 0.15
    congestion_weight: float = 0.10
    safety_weight: float = 0.25
    novelty_weight: float = 0.05
    team_weight: float = 0.05
    difference_weight: float = 0.20     # v2: doc-2's marginal-contribution term
    redundancy_penalty: float = 0.10    # v2: penalize acting on 'claimed' targets
    hard_penalties: dict[str, float] = field(
        default_factory=lambda: {
            "policy_violation": -100.0,
            "unsafe_action": -250.0,
            "stale_data_use": -20.0,
        }
    )
    schema_version: str = SCHEMA_VERSION


@dataclass
class SwarmRecommendation:
    """What flows up from the swarm. The swarm proposes; it never executes."""

    mission_id: str
    swarm_mission_id: str
    recommended_action: dict[str, Any]
    supporting_actions: list[dict[str, Any]]
    expected_reward: float
    confidence: float
    risk_score: float
    requires_approval: bool
    explanation: dict[str, Any] = field(default_factory=dict)
    recommendation_id: str = field(default_factory=lambda: new_id("rec"))
    schema_version: str = SCHEMA_VERSION


# --------------------------------------------------------------------------
# Layer 4 — policy, execution, telemetry
# --------------------------------------------------------------------------


@dataclass
class ActionRequest:
    """POST /v1/policy/authorize-action — the most important interface."""

    mission_id: str
    principal_id: str
    parent_agent_id: str
    action_class: ActionClass
    tool_name: str
    params: dict[str, Any]
    resource_targets: list[str]
    risk_score: float
    confidence: float
    budget_remaining_usd: float = 0.0
    field_snapshot_id: str | None = None
    action_id: str = field(default_factory=lambda: new_id("act"))
    schema_version: str = SCHEMA_VERSION


@dataclass
class PolicyDecision:
    action_id: str
    decision: str                      # allow | deny | require_approval
    policy_id: str
    obligations: list[str] = field(default_factory=list)
    reason: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass
class ExecutionResult:
    action_id: str
    status: str                        # committed | dry_run_ok | denied | failed
    tool_result: dict[str, Any] = field(default_factory=dict)
    observed_effects: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryWrite:
    """POST /v1/memory/write — memory writes are governed events, not side
    effects. In v2 this includes Hermes *skill* writes: skills are executable
    procedural memory, so autonomous skill creation is disabled on governed
    agents and skill persistence flows through this contract instead.
    """

    mission_id: str
    agent_id: str
    memory_scope: str                  # episodic | semantic | procedural | swarm
    title: str
    content: str
    derived_from: list[str] = field(default_factory=list)
    validated: bool = False
    retention_policy: str = "long_term"
    schema_version: str = SCHEMA_VERSION
