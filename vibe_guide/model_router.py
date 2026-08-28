"""Evidence-bound model selection for concrete Issue/Spec work."""

from typing import Iterable, List, Optional, Sequence

from .models import IssueComplexity, LocalModel, WorkerProfile


class WorkerUnavailable(RuntimeError):
    """No safe model can be selected from observable probe facts."""

    def __init__(self, message: str, status: str = "worker_unavailable"):
        super().__init__(message)
        self.status = status


def _needs_deep(issue: IssueComplexity) -> bool:
    return bool(
        {"migration", "security", "cross_module"}.intersection(issue.risk_tags)
        or issue.complexity_band == "complex"
        or issue.context_demand == "large"
        or issue.failure_cost >= 4
    )


class ModelRouter:
    """Select the least powerful verified model satisfying the Issue contract."""

    def __init__(self, worker: str = "developer"):
        if not isinstance(worker, str) or not worker.strip():
            raise ValueError("worker must be non-empty")
        self.worker = worker

    def select(
        self,
        issue_complexity: IssueComplexity,
        required_capabilities: Sequence[str],
        models: Iterable[LocalModel],
    ) -> WorkerProfile:
        if not isinstance(issue_complexity, IssueComplexity):
            raise TypeError("IssueComplexity is required; project S1 cannot route workers")
        if not isinstance(required_capabilities, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in required_capabilities
        ):
            raise TypeError("required_capabilities must be a list of strings")
        required = list(dict.fromkeys(required_capabilities))
        candidates = list(models)
        if not all(isinstance(model, LocalModel) for model in candidates):
            raise TypeError("models must contain LocalModel values")
        if issue_complexity.context_demand == "unknown":
            raise WorkerUnavailable(
                "context demand is unverifiable for %s" % issue_complexity.issue_id,
                "blocked_unknown",
            )
        if not candidates:
            raise WorkerUnavailable(
                "model probe facts are missing for %s" % issue_complexity.issue_id,
                "blocked_unknown",
            )

        deep_required = _needs_deep(issue_complexity)
        suitable: List[LocalModel] = []
        unknown = []
        unavailable = []
        for model in candidates:
            if not set(required).issubset(set(model.capabilities)):
                continue
            if deep_required and "deep" not in model.reasoning_levels:
                continue
            if issue_complexity.context_demand == "large" and model.context_limit < 32_000:
                continue
            if model.available is None:
                unknown.append(model)
            elif model.available:
                suitable.append(model)
            else:
                unavailable.append(model)

        if not suitable:
            if unknown:
                raise WorkerUnavailable(
                    "model availability probe is unverifiable: %s" % ", ".join(m.model_id for m in unknown),
                    "blocked_unknown",
                )
            raise WorkerUnavailable("no available model satisfies IssueComplexity", "worker_unavailable")

        suitable.sort(key=lambda model: (model.context_limit, model.model_id))
        selected = suitable[-1] if deep_required else suitable[0]
        reasoning = "deep" if deep_required else "normal"
        if reasoning not in selected.reasoning_levels:
            raise WorkerUnavailable("selected model lacks required reasoning level")
        fallbacks = [
            {
                "model": model.model_id,
                "reason": "unavailable",
                "availability": model.available,
                "capabilities": list(model.capabilities),
            }
            for model in unavailable
        ]
        basis = {
            "issue_complexity_ref": issue_complexity.issue_id,
            "spec_ref": issue_complexity.spec_ref,
            "steps": issue_complexity.steps,
            "domains": issue_complexity.domains,
            "uncertainty": issue_complexity.uncertainty,
            "failure_cost": issue_complexity.failure_cost,
            "toolchain": issue_complexity.toolchain,
            "context_demand": issue_complexity.context_demand,
            "evidence_ref": issue_complexity.evidence_ref,
            "complexity_band": issue_complexity.complexity_band,
            "risk_tags": list(issue_complexity.risk_tags),
            "required_capabilities": required,
            "reasoning": reasoning,
            "availability_evidence": "probe",
            "context_limit": selected.context_limit,
        }
        return WorkerProfile(
            worker=self.worker,
            model=selected.model_id,
            reasoning=reasoning,
            fallbacks=fallbacks,
            selection_basis=basis,
        )


__all__ = [
    "IssueComplexity",
    "LocalModel",
    "ModelRouter",
    "WorkerProfile",
    "WorkerUnavailable",
]
