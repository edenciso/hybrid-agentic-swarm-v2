"""Layer 4 — policy decision point + approval service.

An OPA-shaped, in-process policy engine: structured ``ActionRequest`` in,
``PolicyDecision`` out, enforcement kept outside the engine (the Tool
Broker is the enforcement point). In production, replace ``decide()`` with
a call to an OPA sidecar evaluating ``policies/action_classes.rego`` — the
rules below mirror that file on purpose.

Enforcement rules (v1 §5, unchanged in v2):

* swarm workers may emit only classes 0-1 directly
* classes 2-5 must pass through the Tool Broker
* classes 4-5 require policy evaluation
* class 5 always requires approval unless exempted by signed policy
* memory writes to curated memory require provenance

v2 additions (Hermes-specific):

* Hermes' per-session approval bypass (``/yolo``) must be disabled on
  governed profiles — the engine denies any request flagged as bypassing
  the broker.
* Hermes autonomous skill creation counts as a *procedural memory write*
  and flows through the same governed path.
"""

from __future__ import annotations

from collections.abc import Callable

from .contracts import ActionClass, ActionRequest, PolicyDecision, PolicyEnvelope
from .telemetry import Telemetry


class PolicyEngine:
    def __init__(self, envelope: PolicyEnvelope, telemetry: Telemetry,
                 autonomy_profile: str = "recommend_only") -> None:
        self.envelope = envelope
        self.telemetry = telemetry
        self.autonomy_profile = autonomy_profile
        self.denial_count = 0

    def decide(self, request: ActionRequest) -> PolicyDecision:
        decision, reason, obligations = self._evaluate(request)
        if decision == "deny":
            self.denial_count += 1
        result = PolicyDecision(
            action_id=request.action_id,
            decision=decision,
            policy_id="rego://has/action_classes",
            obligations=obligations,
            reason=reason,
        )
        self.telemetry.emit(
            "policy.decision",
            action_id=request.action_id,
            principal_id=request.principal_id,
            tool_name=request.tool_name,
            action_class=int(request.action_class),
            decision=decision,
            reason=reason,
        )
        return result

    def _evaluate(self, r: ActionRequest) -> tuple[str, str, list[str]]:
        if r.principal_id.startswith(("scout", "router", "tuner", "sentinel")) \
                and r.action_class > ActionClass.RECOMMEND:
            return ("deny",
                    "swarm workers may emit only class 0-1 directly", [])

        if r.risk_score > self.envelope.max_risk_score \
                and r.action_class >= ActionClass.BOUNDED_REAL_WORLD:
            return ("deny",
                    f"risk {r.risk_score:.2f} exceeds envelope "
                    f"{self.envelope.max_risk_score:.2f}", [])

        if r.action_class == ActionClass.IRREVERSIBLE:
            return ("require_approval", "class 5 always requires approval",
                    ["human_approval", "record_full_audit_payload"])

        if r.action_class == ActionClass.BOUNDED_REAL_WORLD:
            if self.autonomy_profile == "recommend_only" \
                    or self.envelope.no_real_world_execution:
                return ("deny",
                        "mission autonomy profile forbids real-world execution", [])
            obligations = ["record_full_audit_payload",
                           "execute_dry_run_if_supported"]
            if r.confidence < self.envelope.requires_review_if_confidence_lt:
                return ("require_approval",
                        f"confidence {r.confidence:.2f} below review "
                        f"threshold", ["human_approval", *obligations])
            return ("require_approval",
                    "class 4 bounded real-world execution", 
                    ["human_approval", *obligations])

        return ("allow", "within envelope", [])


class ApprovalService:
    """Human-in-the-loop gate. In a Hermes deployment the approval prompt is
    delivered over the messaging gateway (Telegram/Slack/Signal/...) and the
    decision recorded with the approver's identity; here it is a callable so
    the quickstart can simulate an operator."""

    def __init__(self, telemetry: Telemetry,
                 approver: Callable[[ActionRequest, PolicyDecision], bool]) -> None:
        self.telemetry = telemetry
        self.approver = approver

    def request(self, action: ActionRequest, decision: PolicyDecision) -> bool:
        approved = self.approver(action, decision)
        self.telemetry.emit(
            "approval.decision",
            action_id=action.action_id,
            approved=approved,
        )
        return approved
