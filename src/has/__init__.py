"""Hybrid Agentic Swarm (HAS) v2 reference implementation.

Deterministic orchestration (Hermes Agent) as the envelope; emergence
inside it, coupled through one stigmergic substrate; policy and
observability everywhere.
"""

from .contracts import (
    ActionClass,
    ActionRequest,
    AgentSpec,
    FieldSnapshot,
    FieldUpdate,
    MemoryWrite,
    MissionEnvelope,
    PolicyDecision,
    PolicyEnvelope,
    RewardProfile,
    SwarmMission,
    SwarmRecommendation,
    TaskGraph,
    TaskNode,
)
from .guardrails import BreakerConfig, CircuitBreaker, InvariantMonitor
from .orchestrator import (
    GovernedMemory,
    HermesAdapter,
    MasterOrchestrator,
    ToolBroker,
)
from .policy import ApprovalService, PolicyEngine
from .reward import OutcomeFeatures, RewardEngine
from .substrate import DepositRejected, PheromoneField
from .swarm import SwarmMissionBroker
from .telemetry import Telemetry

__version__ = "2.0.0"

__all__ = [
    "ActionClass", "ActionRequest", "AgentSpec", "ApprovalService",
    "BreakerConfig", "CircuitBreaker", "DepositRejected", "FieldSnapshot",
    "FieldUpdate", "GovernedMemory", "HermesAdapter", "InvariantMonitor",
    "MasterOrchestrator", "MemoryWrite", "MissionEnvelope", "OutcomeFeatures",
    "PheromoneField", "PolicyDecision", "PolicyEngine", "PolicyEnvelope",
    "RewardEngine", "RewardProfile", "SwarmMission", "SwarmMissionBroker",
    "SwarmRecommendation", "TaskGraph", "TaskNode", "Telemetry", "ToolBroker",
]
