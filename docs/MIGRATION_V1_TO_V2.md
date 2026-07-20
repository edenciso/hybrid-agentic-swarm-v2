# Migrating HAS v1 (OpenClaw) → v2 (Hermes Agent)

The architecture was designed so the control-plane runtime is swappable: contracts, the substrate, the swarm layer, and the guardrail plane are runtime-agnostic. Migration is therefore mostly a Layer 1 exercise.

## Why the swap works

Hermes Agent occupies the same architectural slot OpenClaw did — a self-hosted, open-source (MIT) agent runtime with per-agent isolation, explicit memory, a skills system, and a gateway — and it ships several things v1 had to specify externally: credential brokering (Bitwarden Secrets Manager), sandbox isolation as a first-class choice (six terminal backends including Docker, Modal, Daytona), context-window injection defense (promptware chokepoint scanning of tool output, recalled memory, and stored skills), an approval delivery surface (the messaging gateway), and an RL training substrate (Atropos + trajectory export) that Layer 3 can use directly.

## Practical migration path

Hermes' setup wizard auto-detects an existing `~/.openclaw` installation and offers to migrate settings, memories, skills, and API keys before configuration begins (`hermes setup`). For governed HAS deployments, do **not** accept a wholesale import: migrate per-agent, starting each governed profile from **Blank Slate** setup, then re-introduce only the tools, skills, and credentials that the agent's `AgentSpec` names. A bulk import would silently re-enable capabilities the v1 policy plane had scoped away.

## Concept mapping

| v1 concept (OpenClaw) | v2 realization (Hermes Agent) | Action required |
|---|---|---|
| per-agent workspace / agentDir / session store | profile per governed agent; isolated subagents | recreate profiles from Blank Slate |
| tool allow/deny per agent | toolset config + MCP tool filtering | encode in `AgentSpec.toolset_allow/deny` |
| per-agent auth profiles | Bitwarden Secrets Manager scopes | move keys out of env files; one bootstrap token |
| sandbox config | terminal backend selection | `docker`/`modal`/`daytona` for tool executors; never `local` |
| Markdown workspace memory | agent-curated memory + FTS5 recall | keep the governed memory-write contract as the only curated-write path |
| hand-authored skills | learning loop: autonomous skill creation + self-improvement | **disable on governed agents**; route skill persistence through `MemoryWrite(scope="procedural")` |
| advisory prompt guardrails | promptware defense at three chokepoints | keep enabled; do not treat as a replacement for the OPA plane |
| n/a | `/yolo` per-session approval bypass | disable on governed profiles |
| n/a | per-task model overrides / smart routing | use for the cheap-worker / heavy-reasoner split |
| n/a | messaging gateway (20+ platforms) | wire the Approval Service to it |
| n/a | cron | scheduled swarm missions |
| n/a | Atropos RL + trajectory export | Layer 3 offline training and shadow-mode evaluation |

## What does not change

The interface contracts (all of `docs/CONTRACTS.md` except the spawn contract's runtime fields). The stigmergic substrate and its defenses. The worker archetypes, algorithms, and reward design. The action-class ladder and OPA policy. OTel instrumentation and the replay store. The governance caveat also carries over unchanged: Hermes, like OpenClaw, is an operator-centric runtime, not an enterprise multi-user RBAC platform — identity, policy decisions, and enforcement stay externalized (IdP + OPA + Tool Broker).

## New risk surfaces to review in v2

Two capabilities that make Hermes attractive also widen the attack/failure surface and need explicit governance. The **learning loop**: autonomous skill creation means the agent can write executable procedural memory for itself; on governed agents this must be off, with skill writes flowing through the provenance-checked memory contract (Hermes' own promptware defense scanning *stored skills* as an injection chokepoint confirms the concern). The **gateway**: 20+ inbound messaging surfaces are 20+ prompt-injection ingress points; restrict governed agents' gateway exposure to the approval channel only.
