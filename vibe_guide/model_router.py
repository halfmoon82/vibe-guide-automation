"""Evidence-bound routing from one IssueComplexity to a local worker model.

The router deliberately accepts an :class:`IssueComplexity` record rather than
the project S1 score.  Model availability is treated as a probe fact: a
negative probe can be bypassed through a recorded fallback, while an unknown
probe remains ``blocked_unknown`` and is never silently treated as unavailable.
"""

from typing import Any, Dict, Iterable, List, Sequence

from .models import IssueComplexity, LocalModel, WorkerProfile


class WorkerUnavailable(RuntimeError):
    """No safely selectable worker model exists for the requested Issue."""

    def __init__(self, status: str, reason: str = ""):
        if status not in {"worker_unavailable", "blocked_unknown"}:
            raise ValueError("unsupported worker status")
        self.status = status
        self.reason = reason or status
        super().__init__(self.reason)


_HIGH_RISK_TAGS = frozenset({"migration", "security", "cross_module"})
_CONTEXT_REQUIREMENTS = {
    "small": 1,
    "medium": 16_000,
    "large": 32_000,
}


def _validate_capabilities(required: Sequence[str]) -> List[str]:
    if not isinstance(required, (list, tuple)):
        raise TypeError("required_capabilities must be a list")
    result = []
    for value in required:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("required capabilities must be non-empty strings")
        value = value.strip()
        if value not in result:
            result.append(value)
    return result


def _desired_reasoning(issue: IssueComplexity) -> str:
    if issue.complexity_band == "complex" or _HIGH_RISK_TAGS.intersection(issue.risk_tags):
        return "deep"
    return "normal"


def _context_limit(issue: IssueComplexity) -> int:
    try:
        return _CONTEXT_REQUIREMENTS[issue.context_demand]
    except KeyError as error:
        raise WorkerUnavailable(
            "blocked_unknown", "context demand is unverifiable for %s" % issue.issue_id
        ) from error


def _supports(model: LocalModel, capabilities: Sequence[str], reasoning: str) -> bool:
    return (
        set(capabilities).issubset(set(model.capabilities))
        and reasoning in model.reasoning_levels
    )


class ModelRouter:
    """Select a deterministic local model and emit a replayable WorkerProfile."""

    def __init__(self, worker: str = "local"):
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError("worker must be non-empty")
        self.worker = worker.strip()

    def select(
        self,
        issue_complexity: IssueComplexity,
        required_capabilities: Sequence[str],
        models: Iterable[LocalModel],
    ) -> WorkerProfile:
        if not isinstance(issue_complexity, IssueComplexity):
            raise TypeError("issue_complexity must be an IssueComplexity")
        capabilities = _validate_capabilities(required_capabilities)
        if not isinstance(models, (list, tuple)):
            raise TypeError("models must be a list")
        if not models or not all(isinstance(model, LocalModel) for model in models):
            raise TypeError("models must contain LocalModel values")

        desired_reasoning = _desired_reasoning(issue_complexity)
        minimum_context = _context_limit(issue_complexity)
        candidates = [
            model
            for model in models
            if _supports(model, capabilities, desired_reasoning)
        ]
        if not candidates:
            raise WorkerUnavailable(
                "worker_unavailable",
                "no model satisfies required capabilities, reasoning, and context",
            )

        unknown = [model for model in candidates if model.available is None]
        if unknown:
            names = ",".join(model.model_id for model in unknown)
            raise WorkerUnavailable(
                "blocked_unknown", "model availability probe is unknown: %s" % names
            )

        available = [model for model in candidates if model.available is True]
        unavailable = [model for model in candidates if model.available is False]
        if not available:
            names = ",".join(model.model_id for model in unavailable)
            raise WorkerUnavailable(
                "worker_unavailable", "all compatible models are unavailable: %s" % names
            )

        context_fit = [model for model in available if model.context_limit >= minimum_context]
        ranked = context_fit or available
        if issue_complexity.complexity_band == "complex":
            selected = max(ranked, key=lambda model: (model.context_limit, model.model_id))
        else:
            selected = min(ranked, key=lambda model: (model.context_limit, model.model_id))

        fallbacks: List[Dict[str, Any]] = []
        for model in candidates:
            if model.model_id == selected.model_id:
                continue
            fallbacks.append(
                {
                    "model": model.model_id,
                    "reasoning": desired_reasoning,
                    "availability": (
                        "available" if model.available is True else "unavailable"
                    ),
                    "availability_evidence": "probe:%s" % model.model_id,
                    "context_fit": model.context_limit >= minimum_context,
                }
            )

        selection_basis = {
            "issue_complexity_ref": issue_complexity.issue_id,
            "spec_ref": issue_complexity.spec_ref,
            "steps": issue_complexity.steps,
            "domains": issue_complexity.domains,
            "uncertainty": issue_complexity.uncertainty,
            "failure_cost": issue_complexity.failure_cost,
            "toolchain": issue_complexity.toolchain,
            "complexity_band": issue_complexity.complexity_band,
            "risk_tags": list(issue_complexity.risk_tags),
            "required_capabilities": list(capabilities),
            "context_demand": issue_complexity.context_demand,
            "minimum_context": minimum_context,
            "reasoning_rule": (
                "risk_or_complexity_escalation"
                if desired_reasoning == "deep"
                else "issue_complexity_default"
            ),
            "availability_evidence": "probe:%s" % selected.model_id,
            "evidence_ref": issue_complexity.evidence_ref,
        }
        return WorkerProfile(
            worker=self.worker,
            model=selected.model_id,
            reasoning=desired_reasoning,
            fallbacks=fallbacks,
            selection_basis=selection_basis,
        )


__all__ = [
    "IssueComplexity",
    "LocalModel",
    "ModelRouter",
    "WorkerProfile",
    "WorkerUnavailable",
]
