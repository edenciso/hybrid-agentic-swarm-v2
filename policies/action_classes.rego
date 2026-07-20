# HAS v2 action-authorization policy (OPA / Rego)
#
# The in-process engine in src/has/policy.py mirrors these rules; in a
# production deployment the Tool Broker queries an OPA sidecar with the
# ActionRequest JSON (see docs/CONTRACTS.md §Action Authorization) and this
# package returns {decision, obligations}.
#
# Action classes:
#   0 observe · 1 recommend · 2 simulate · 3 reversible ·
#   4 bounded real-world · 5 irreversible

package has.action_classes

import rego.v1

default decision := "deny"

worker_prefixes := ["scout", "router", "tuner", "sentinel"]

is_swarm_worker if {
	some prefix in worker_prefixes
	startswith(input.requester.principal_id, prefix)
}

# Swarm workers may emit only classes 0-1 directly.
worker_overreach if {
	is_swarm_worker
	input.action.action_class > 1
}

risk_over_envelope if {
	input.action.action_class >= 4
	input.context.risk_score > data.envelope.max_risk_score
}

real_world_forbidden if {
	input.action.action_class == 4
	data.envelope.no_real_world_execution == true
}

decision := "allow" if {
	not worker_overreach
	not risk_over_envelope
	not real_world_forbidden
	input.action.action_class <= 3
}

decision := "require_approval" if {
	not worker_overreach
	not risk_over_envelope
	not real_world_forbidden
	input.action.action_class >= 4
}

obligations contains "human_approval" if decision == "require_approval"

obligations contains "record_full_audit_payload" if input.action.action_class >= 4

obligations contains "execute_dry_run_if_supported" if input.action.action_class == 4

obligations contains "human_approval" if {
	input.action.action_class == 4
	input.context.confidence < data.envelope.requires_review_if_confidence_lt
}
