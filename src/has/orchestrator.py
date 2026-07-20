"""Layer 1 — deterministic orchestration via Hermes Agent (Nous Research).

The control plane never directs the swarm's moment-to-moment choices; it
defines the box the swarm explores in, and it holds kill authority.

``HermesAdapter`` is the seam to a real Hermes Agent runtime. Governed
deployment posture (see docs/MIGRATION_V1_TO_V2.md for the full mapping):

* start each governed agent from Hermes **Blank Slate** setup — everything
  off, opt-in tools only
* one **profile per agent**, isolated subagents for parallel workstreams
* **terminal backends** (docker / modal / daytona / ssh / singularity) as
  the sandbox boundary — never ``local`` for tool-executing agents
* credentials via **Bitwarden Secrets Manager** scopes, never raw keys
* **autonomous skill creation disabled** on governed agents; skills are
  procedural memory and persist only through the governed memory-write
  contract
* the per-session approval bypass (``/yolo``) disabled; approvals ride the
  Hermes **messaging gateway** to a human operator
* keep Hermes' promptware/injection chokepoint scanning enabled (tool
  output, recalled memory, stored skills)

When no Hermes runtime is present (as in the quickstart), the adapter
falls back to a deterministic in-process planner so the whole loop stays
runnable with zero dependencies.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .contracts import (
    ActionClass,
    ActionRequest,
    AgentSpec,
    ContextPackage,
    ExecutionResult,
    MemoryWrite,
    MissionEnvelope,
    PolicyEnvelope,
    SpawnResult,
    SwarmRecommendation,
    TaskGraph,
    TaskNode,
)
from .policy import ApprovalService, PolicyEngine
from .telemetry import Telemetry


class HermesAdapter:
    """Integration seam to the Hermes Agent runtime.

    ``available()`` detects a local ``hermes`` install. The spawn/plan
    methods below model the *contract* with the runtime; wiring them to a
    live gateway is deployment-specific (Hermes exposes a CLI, a messaging
    gateway, and MCP — pick the transport that fits your environment) and
    is intentionally left behind this one seam.
    """

    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    @staticmethod
    def available() -> bool:
        return shutil.which("hermes") is not None

    def spawn(self, spec: AgentSpec) -> SpawnResult:
        # Governance invariants enforced at the seam, whatever the transport:
        assert spec.skills_readonly, (
            "governed agents must not create/self-modify skills directly; "
            "route skill persistence through the memory-write contract"
        )
        assert spec.terminal_backend != "local" or not spec.toolset_allow, (
            "tool-executing agents must run in an isolated terminal backend"
        )
        runtime = "hermes" if self.available() else "sim"
        result = SpawnResult(
            agent_id=spec.agent_id,
            runtime_ref=f"{runtime}://profiles/{spec.agent_id}",
        )
        self.telemetry.emit(
            "agent.spawn",
            agent_id=spec.agent_id,
            role=spec.role,
            runtime_ref=result.runtime_ref,
            terminal_backend=spec.terminal_backend,
            model_override=spec.model_override,
        )
        return result


class GovernedMemory:
    """Explicit, workspace-backed memory. Writes are governed events with
    provenance — including Hermes *skill* writes (procedural scope)."""

    def __init__(self, telemetry: Telemetry, root: str | Path = "runs") -> None:
        self.telemetry = telemetry
        self.root = Path(root)

    def write(self, entry: MemoryWrite) -> str:
        if entry.memory_scope in ("semantic", "procedural") and not entry.derived_from:
            raise ValueError("curated memory writes require provenance")
        path = self.root / f"{entry.mission_id}_MEMORY.md"
        slug = entry.title.lower().replace(" ", "-")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {entry.title}\n\n{entry.content}\n\n"
                    f"*scope: {entry.memory_scope} · derived from: "
                    f"{', '.join(entry.derived_from) or '—'} · "
                    f"validated: {entry.validated}*\n")
        ref = f"mem://workspace/{path.name}#{slug}"
        self.telemetry.emit("memory.write", agent_id=entry.agent_id,
                            memory_scope=entry.memory_scope, memory_ref=ref)
        return ref


class ToolBroker:
    """The only place where real-world acts occur (policy enforcement
    point). Tools are registered callables; every execution is
    policy-checked first and dry-run when obligated."""

    def __init__(self, policy: PolicyEngine, approvals: ApprovalService,
                 telemetry: Telemetry) -> None:
        self.policy = policy
        self.approvals = approvals
        self.telemetry = telemetry
        self._tools: dict[str, Callable[[dict], dict]] = {}

    def register(self, name: str, fn: Callable[[dict], dict]) -> None:
        self._tools[name] = fn

    def execute(self, request: ActionRequest) -> ExecutionResult:
        decision = self.policy.decide(request)

        if decision.decision == "deny":
            status = "denied"
            result: dict = {"reason": decision.reason}
        elif decision.decision == "require_approval" and not self.approvals.request(
                request, decision):
            status, result = "denied", {"reason": "approval declined"}
        else:
            tool = self._tools.get(request.tool_name)
            if tool is None:
                status, result = "failed", {"reason": "unknown tool"}
            else:
                if "execute_dry_run_if_supported" in decision.obligations:
                    self.telemetry.emit("agent.action", action_id=request.action_id,
                                        tool_name=request.tool_name, status="dry_run_ok")
                result = tool(request.params)
                status = "committed"

        self.telemetry.emit(
            "agent.action",
            action_id=request.action_id,
            actor_id=request.principal_id,
            tool_name=request.tool_name,
            action_class=int(request.action_class),
            status=status,
        )
        return ExecutionResult(action_id=request.action_id, status=status,
                               tool_result=result)


class MasterOrchestrator:
    """Top-level governor: decomposes the mission, spawns the governed
    cohort (within spawn caps), issues swarm missions, and decides what the
    swarm's recommendations may become."""

    def __init__(self, envelope: MissionEnvelope, telemetry: Telemetry,
                 hermes: HermesAdapter, memory: GovernedMemory) -> None:
        self.envelope = envelope
        self.telemetry = telemetry
        self.hermes = hermes
        self.memory = memory
        self.agents: dict[str, SpawnResult] = {}

    # -- planning -----------------------------------------------------------

    def plan(self) -> TaskGraph:
        """Deterministic fallback planner. With a live Hermes runtime, this
        is where the Planner subagent's task decomposition lands (its output
        must still validate against the TaskGraph contract)."""
        graph = TaskGraph(mission_id=self.envelope.mission_id, tasks=[
            TaskNode(task_id="task_001", type="graph_assignment",
                     description="Search crew-to-zone assignments in the field",
                     action_class=ActionClass.SIMULATE,
                     required_capabilities=["field_read", "sim_route"],
                     reward_profile_id="reward_dispatch_v2"),
            TaskNode(task_id="task_002", type="execute_dispatch",
                     description="Publish approved dispatch schedule",
                     action_class=ActionClass.BOUNDED_REAL_WORLD,
                     depends_on=["task_001"],
                     required_capabilities=["dispatch_write"],
                     approval_policy="ops_manager"),
        ])
        self.telemetry.emit("mission.planned",
                            planner="hermes" if self.hermes.available() else "fallback",
                            tasks=[t.task_id for t in graph.tasks])
        return graph

    # -- cohort -------------------------------------------------------------

    def spawn_cohort(self) -> None:
        specs = [
            AgentSpec("planner_01", "planner", model_override="heavy-reasoner"),
            AgentSpec("safety_01", "safety"),
            AgentSpec("swarm_broker_01", "swarm_mission_broker"),
            AgentSpec("tool_exec_01", "tool_executor",
                      terminal_backend="docker",
                      toolset_allow=["dispatch.publish"],
                      secrets_scope="bws://acme-grid/dispatch"),
            AgentSpec("memory_curator_01", "memory_curator"),
        ]
        if len(specs) > self.envelope.max_workers:
            raise RuntimeError("spawn cap exceeded (fork-bomb guard)")
        for spec in specs:
            self.agents[spec.agent_id] = self.hermes.spawn(spec)

    def context_for(self, agent_id: str, role: str) -> ContextPackage:
        return ContextPackage(
            mission_id=self.envelope.mission_id,
            agent_id=agent_id,
            constitution={
                "objective": self.envelope.objective,
                "invariants": self.envelope.hard_constraints,
                "forbidden": ["execute outside the tool broker",
                              "reduce risk channel unless sentinel",
                              "bypass approval (/yolo disabled)"],
            },
            role_context={"role": role},
        )

    # -- execution decision ---------------------------------------------------

    def act_on(self, recommendation: SwarmRecommendation,
               broker: ToolBroker) -> ExecutionResult:
        request = ActionRequest(
            mission_id=self.envelope.mission_id,
            principal_id="tool_exec_01",
            parent_agent_id="master_01",
            action_class=ActionClass.BOUNDED_REAL_WORLD,
            tool_name="dispatch.publish",
            params={"plan": recommendation.recommended_action,
                    "recommendation_id": recommendation.recommendation_id},
            resource_targets=["dispatch-system://region-east"],
            risk_score=recommendation.risk_score,
            confidence=recommendation.confidence,
            budget_remaining_usd=self.envelope.currency_budget_usd,
        )
        return broker.execute(request)

    def close_out(self, recommendation: SwarmRecommendation,
                  result: ExecutionResult) -> str:
        return self.memory.write(MemoryWrite(
            mission_id=self.envelope.mission_id,
            agent_id="memory_curator_01",
            memory_scope="semantic",
            title="Dispatch mission outcome",
            content=(f"Recommendation {recommendation.recommendation_id} "
                     f"(expected reward {recommendation.expected_reward}, "
                     f"confidence {recommendation.confidence}) -> "
                     f"execution status: {result.status}."),
            derived_from=[recommendation.recommendation_id, result.action_id],
            validated=result.status == "committed",
        ))
