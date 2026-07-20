# HAS v2 Interface Contract Spec

The v1 contract spec carries into v2 nearly unchanged — that was the point of externalizing the contracts from the runtime. This page lists the invariants, the contract objects, and what changed. The executable definitions live in [`src/has/contracts.py`](../src/has/contracts.py); JSON examples below use the same field names.

## Core design rules (unchanged)

Every request carries `mission_id`. Every actor has an `agent_id` or `worker_id`. Every real action carries an `action_class`. Every stateful change carries a `causation_id`. Every contract object has a `schema_version` (now `"2.0"`). Every mutable object has TTL or expiration semantics where relevant. Every execution request is policy-checkable from structured input.

## Contract inventory

| Contract | Endpoint shape | v2 delta |
|---|---|---|
| MissionEnvelope | `POST /v1/missions` | adds `max_workers`, `max_spawn_depth` (fork-bomb guards) |
| TaskGraph / TaskNode | internal | unchanged |
| AgentSpec / SpawnResult | `POST /v1/agents/spawn` | re-targeted to Hermes: `terminal_backend`, `secrets_scope` (Bitwarden), `model_override`, `skills_readonly`; `runtime_ref` is now `hermes://profiles/<agent_id>` |
| ContextPackage | internal | adds the immutable `constitution` tier |
| SwarmMission | `POST /v1/swarm/missions` | field channel list extended (below) |
| FieldSnapshot / FieldUpdate | `POST /v1/field/query`, `POST /v1/field/update` | unchanged shape; substrate now enforces delta bounds, rate limits, saturate guard, sentinel-only risk reduction |
| RewardProfile | internal | adds `difference_weight`, `redundancy_penalty` |
| SwarmRecommendation | internal | `explanation` now includes `marginal_contributions` |
| ActionRequest / PolicyDecision | `POST /v1/policy/authorize-action` | unchanged (the most important interface in the system) |
| ExecutionResult | `POST /v1/tools/execute` | unchanged |
| MemoryWrite | `POST /v1/memory/write` | `memory_scope` gains `procedural` — Hermes skill writes flow through this contract |
| Telemetry events | OTel | event set unchanged: `agent.action`, `field.update`, `policy.decision`, plus v2 `breaker.state`, `memory.write`, `approval.decision` |

## Field channels (v2 unified set)

`intent`, `success`, `risk`, `congestion`, `cost`, `trust`, `novelty` (from the v1 formal spec), plus `claimed` (anti-redundancy — another worker is already here) and `evidence` (observed ground truth, distinct from optimistic reinforcement) from the v1 conceptual spec.

## Example: agent spawn (v2)

```json
{
  "schema_version": "2.0",
  "mission_id": "mis_01JZJQ7N9X",
  "parent_agent_id": "master_01",
  "agent_spec": {
    "agent_id": "tool_exec_01",
    "role": "tool_executor",
    "terminal_backend": "docker",
    "toolset_allow": ["dispatch.publish"],
    "toolset_deny": ["browser.payments"],
    "secrets_scope": "bws://acme-grid/dispatch",
    "model_override": null,
    "skills_readonly": true,
    "context_scope": ["mission", "role", "session"]
  }
}
```

Response: `{"agent_id": "tool_exec_01", "runtime_ref": "hermes://profiles/tool_exec_01", "status": "ready"}`

## Example: action authorization (unchanged from v1)

Request carries requester identity, originating mission id, sub-agent id, tool name, params hash, resource targets, risk score, confidence, budget consumed, and the field snapshot id. Response carries `decision` (`allow` | `deny` | `require_approval`), `policy_id`, and `obligations` such as `human_approval`, `record_full_audit_payload`, `execute_dry_run_if_supported`. OPA evaluates `policies/action_classes.rego` against this structured input; enforcement stays in the Tool Broker.

## Field update invariants (v2-hardened)

Deltas are bounded per channel (`max_field_delta_per_update`). Per-worker deposits are rate-limited in a sliding window. The risk channel cannot be reduced by non-sentinel workers. Updates are rejected once the mission namespace closes. Positive deposits pass through the saturate guard. Every applied update emits a telemetry event with provenance; every rejected update increments a counter the circuit breaker watches.

## Memory write invariants

Curated scopes (`semantic`, `procedural`) require provenance (`derived_from` must be non-empty). Procedural writes are how Hermes skills persist on governed agents — autonomous skill creation is disabled, so skills enter the workspace only through this governed, provenance-tagged path.
