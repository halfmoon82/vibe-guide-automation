"""Publish a confirmed product spec as a plan with its authorization card.

Split out of ``cli`` so the command wiring stays thin.  The helpers that other
CLI commands also use (JSON IO, plan root, capability observation) stay in
``cli`` and are reached through ``_cli()`` at call time: ``publish`` is imported
by ``cli``, so a top-level import back would be circular, and a call-time lookup
keeps those helpers patchable where they are defined.
"""

import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .adapters.task_provider import ProviderActionStore, ProviderPending
from .authorization import (
    DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
    DEFAULT_ROTATE_HARD_CAP_TOKENS,
    AuthorizationCard,
    build_authorization_card,
)
from .dag import append_integration_review_node, render_plan_artifacts, validate_dag
from .engine_attestation import create_engine_attestation
from .models import DAGNode, Plan, AgentCapabilities
from .node_spec import (
    complete_node_contracts,
    render_planning_brief_markdown,
    render_prd_markdown,
)
from .paths import ProjectPaths
from .planner import PRD, DecisionCard, approve_prd
from .session_entry import replaceable_draft_extras
from .supervisor import context_window_from_record, heartbeat_interval_options, rotate_hard_cap_options
from .task_registry import default_topology

DISPATCH_MODES = ("background", "visible")


def _cli():
    from . import cli

    return cli


def _default_heartbeat_minutes(options) -> int:
    """The longest tier this platform may use without a forced-cost choice."""
    usable = [
        entry["minutes"] for entry in options
        if entry.get("available") and not entry.get("forced")
    ]
    return max(usable) if usable else DEFAULT_HEARTBEAT_INTERVAL_MINUTES


def _dispatch_rulings(observed: Dict[str, str], agent_id: str, mode: Optional[str]) -> Dict[str, str]:
    """Apply the platform's default dispatch mode to the observed rulings.

    A platform whose default topology is background (Claude Code) is ruled
    background unless the product spec chose ``visible``; every other platform
    keeps exactly what its recorded capability evidence ruled.
    """
    if mode is not None and mode not in DISPATCH_MODES:
        raise ValueError("dispatch_mode must be one of: " + ", ".join(DISPATCH_MODES))
    rulings = dict(observed or {})
    if mode == "visible":
        if rulings.get(agent_id) == "background":
            del rulings[agent_id]
    elif default_topology(agent_id) == "background":
        rulings[agent_id] = "background"
    return rulings


def _replace_draft(destination: Path, staging: Path) -> None:
    """Swap a published staging directory in for a session draft.

    The draft is moved aside first and judged again there: whatever landed
    in it before the move (another publication, a run) is caught while the
    draft can still be put back.  Files the agent kept in the draft (the
    product spec) move along; a name the publication itself writes is a
    collision and aborts.  Any failure restores the draft.
    """
    holding = Path(tempfile.mkdtemp(prefix="." + destination.name + ".draft.", dir=str(destination.parent)))
    parked = holding / "draft"
    try:
        os.rename(str(destination), str(parked))
    except BaseException:
        # rmdir, not rmtree: an interrupt landing after the rename succeeded
        # leaves the draft in here, and it must stay recoverable.
        try:
            os.rmdir(str(holding))
        except OSError:
            pass
        raise
    try:
        extras = replaceable_draft_extras(parked)
        if extras is None:
            raise FileExistsError("plan already exists")
        for extra in extras:
            target = staging / extra.name
            if target.exists() or target.is_symlink():
                raise FileExistsError("plan already exists: draft file {} collides with the publication".format(extra.name))
            shutil.copy2(str(extra), str(target))
        os.replace(str(staging), str(destination))
    except BaseException:
        try:
            os.rename(str(parked), str(destination))
        except OSError as error:
            raise OSError("draft could not be restored; it is kept at {}".format(parked)) from error
        shutil.rmtree(str(holding), ignore_errors=True)
        raise
    shutil.rmtree(str(holding), ignore_errors=True)


def publish_plan(
    paths: ProjectPaths, plan_id: str, source: Any, session_record: Optional[str] = None
) -> Tuple[Plan, List[DAGNode], AuthorizationCard]:
    # Callers hand over a normalized spec dict; a path is still accepted for
    # the legacy direct-publish callers.
    if isinstance(source, (str, Path)):
        source = _cli()._read_json(Path(source))
    if not isinstance(source, dict):
        raise ValueError("node spec must be a JSON object")
    decisions = [DecisionCard(**item) for item in source.get("decisions", [])]
    prd = PRD(
        str(source.get("title", "")).strip(),
        str(source.get("objective", "")).strip(),
    )
    if not prd.title or not prd.objective:
        raise ValueError("approved PRD title and objective are required")
    approval = approve_prd(prd, decisions)
    if not approval.approved:
        raise PermissionError("product decisions remain unresolved")
    raw_nodes = source.get("nodes", [])
    # The band is settled by node_spec.normalize_node_spec from the session
    # route; a spec carrying an integration contract is complex by definition.
    is_complex_spec = source.get("complexity_band") == "complex" or bool(source.get("integration_contract"))
    source_capabilities = AgentCapabilities.from_dict(source.get("capabilities", {}))
    project_id = source.get("project_id")
    # Visible task routing requires a confirmed project id. Legacy/background
    # node specs predate that field and remain valid because they do not use
    # the visible desktop-task route.
    if source_capabilities.level == "full" and (not isinstance(project_id, str) or not project_id.strip()):
        raise ValueError("node spec project_id is required for visible task routing")
    if not isinstance(project_id, str) or not project_id.strip():
        project_id = None
    complete_node_contracts(raw_nodes, source_capabilities.agent_id, project_id.strip() if project_id else None)
    nodes = [DAGNode.from_dict(item) for item in raw_nodes]
    if not nodes:
        raise ValueError("node spec must contain at least one node")
    validation = validate_dag(nodes)
    if not validation.valid:
        raise ValueError("DAG is invalid: " + "; ".join(validation.errors))

    destination = _cli()._plan_root(paths, plan_id)
    plan = Plan(
        plan_id,
        1,
        str(Path(".vibe") / "plans" / plan_id / "prd.md"),
        [node.id for node in nodes],
        "confirmed_pending_authorization",
        authorization_required=True,
        decisions=[asdict(item) for item in decisions],
        nodes=nodes,
        complexity_band="complex" if is_complex_spec else "",
        spec_path=str(source.get("spec_path", "spec.md" if is_complex_spec else "")),
        integration_contract=source.get("integration_contract", {}),
        implementation_plan_path=str(Path(".vibe") / "plans" / plan_id / "planning-brief.md"),
    )
    if is_complex_spec:
        plan = append_integration_review_node(plan)
    nodes = list(plan.nodes)
    capabilities = source_capabilities
    engine_attestation = None
    try:
        if is_complex_spec:
            observed_store = ProviderActionStore(paths).capabilities(
                capabilities.agent_id
            )
            engine_attestation = create_engine_attestation(
                plan.plan_id,
                plan.version,
                "vibeguide_monitor",
                "dag",
                observed_store["adapter_id"],
                observed_store["facts"],
                observed_store["provenance"],
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        capabilities = _cli()._observed_adapter(paths, capabilities.agent_id).capabilities
    except (FileNotFoundError, OSError, TypeError, ValueError, ProviderPending):
        if is_complex_spec:
            raise PermissionError("engine_attestation_unavailable: verified Monitor engine evidence is required")
        # Planning remains usable for explicitly injected/background test paths;
        # the public monitor rechecks live observed capability before execution.
        pass
    _first_card_rulings = _dispatch_rulings(
        _cli()._observed_topology_rulings(paths),
        capabilities.agent_id,
        source.get("dispatch_mode"),
    )
    # ISSUE-209: the card offers both operational choices with the availability
    # this publication actually observed, and records the product manager's
    # selection.  Availability comes from the platform's registered push
    # wake-up and from the context window the publishing session reports; an
    # unusable tier is listed with its reason, never hidden.
    _heartbeat_options = heartbeat_interval_options(paths, capabilities.agent_id)
    _rotate_options = rotate_hard_cap_options(context_window_from_record(session_record))
    # An explicit ``null`` in the spec means "not stated", not "record nothing":
    # a fresh publication always records a choice, and only a card written
    # before the option existed is allowed to carry none.
    _heartbeat_choice = source.get("heartbeat_interval_minutes")
    _rotate_choice = source.get("rotate_hard_cap")
    if _heartbeat_choice is None:
        # Publishing never asks for an engineering parameter: the long
        # fallback applies when this platform's push wake-up is verified.
        _heartbeat_choice = _default_heartbeat_minutes(_heartbeat_options)
    card = build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=source.get("active_pair_limit"),
        allowed_actions=source.get("allowed_actions"),
        remote_git_actions=source.get("remote_git_actions", "deny"),
        engine_attestation=engine_attestation,
        topology_rulings=_first_card_rulings,
        workers=_cli()._background_worker_entries(nodes, _first_card_rulings, paths),
        heartbeat_interval_minutes=_heartbeat_choice,
        rotate_hard_cap=(
            DEFAULT_ROTATE_HARD_CAP_TOKENS if _rotate_choice is None else _rotate_choice
        ),
        heartbeat_interval_options=_heartbeat_options,
        rotate_hard_cap_options=_rotate_options,
    )

    plans_root = destination.parent
    if plans_root.is_symlink():
        raise ValueError("plans directory may not be a symlink")
    plans_root.mkdir(parents=True, exist_ok=True)
    # A session draft under the same id never held an authorization, so the
    # protocol publishes over it; anything further along is never replaced.
    # _plan_root resolves symlinks, so look at the id's own entry: an alias
    # must never publish over the draft it points to.
    is_draft = False
    if (paths.vibe / "plans" / plan_id).is_symlink():
        raise FileExistsError("plan already exists")
    if destination.exists() or destination.is_symlink():
        if replaceable_draft_extras(destination) is None:
            raise FileExistsError("plan already exists")
        is_draft = True
    staging = Path(tempfile.mkdtemp(prefix="." + plan_id + ".", dir=str(plans_root)))
    try:
        render_plan_artifacts(plan, staging)
        (staging / "specs").mkdir()
        (staging / "issues").mkdir()
        if engine_attestation is not None:
            _cli()._atomic_json(staging / "engine-attestation.json", engine_attestation)
        (staging / "prd.md").write_text(
            render_prd_markdown(source, decisions, plan.evidence_priority), encoding="utf-8",
        )
        (staging / "planning-brief.md").write_text(
            render_planning_brief_markdown(plan_id, source, plan), encoding="utf-8",
        )
        for node in nodes:
            contract = node.contract
            (staging / "specs" / (node.id + ".md")).write_text(
                "# Spec: {}\n\nnode_id: {}\n状态：published\n审核：reviewed\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                    node.title, node.id,
                    contract.get("input"),
                    contract.get("output"),
                    contract.get("error_behavior"),
                    contract.get("acceptance_example"),
                ),
                encoding="utf-8",
            )
            (staging / "issues" / (node.id + ".md")).write_text(
                "# Issue: {}\n\nissue_id: {}\n状态：published\n审核：reviewed\n\n并行组：{}\n".format(
                    node.title, node.id, node.parallel_group or "none"
                ),
                encoding="utf-8",
            )
        _cli()._atomic_json(staging / "plan.json", plan.to_dict())
        _cli()._atomic_json(staging / "nodes.json", [node.to_dict() for node in nodes])
        _cli()._atomic_json(staging / "authorization-card.json", card.to_dict())
        _cli()._atomic_json(staging / "dag-audit.json", {"status": "reviewed", "node_count": len(nodes), "plan_revision": str(plan.version), "node_ids": [node.id for node in nodes]})
        _cli()._atomic_json(staging / "plan-confirmation.json", {"status": "confirmed", "plan_id": plan.plan_id, "plan_revision": str(plan.version), "authorization_digest": card.digest, "authorization_required": True})
        if is_draft:
            _replace_draft(destination, staging)
        else:
            os.replace(str(staging), str(destination))
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return plan, nodes, card
