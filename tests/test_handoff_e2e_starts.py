"""Platform handoff from three starting points, on three platforms, four situations.

PRD ``platform-e2e``.  Starting points (what the run has been through before the
handoff that is judged):

* ``never_failed`` -- the first handoff attempt;
* ``failed_once`` -- the first attempt died half way (the disk refused the node
  file after the run had already moved), then the supervisor tries again;
* ``rerun`` -- the handoff finished, and is issued once more.

Situations: the dispatch mode stays; visible becomes an in-session subagent;
visible becomes background; a worker is running at handoff time.  Everything is
driven through ``vibe attest`` / ``plan`` / ``authorize`` / ``monitor`` and the
real provider mailbox; the judged facts are what a user sees:

1. the card's platform and dispatch mode equal the task registry's;
2. a node has at most one non-terminal binding;
3. a node waiting for its worker has exactly one unclaimed ``create``.

The background combination cannot be finished here: starting a background
worker needs the real ``claude --bg`` launch, which is a real-machine item
(outside this node).  Those tests check what the card and the contract say and
carry one expected failure each for the part that needs the real launch.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import cli as cli_module
from vibe_guide.state import load_events
from vibe_guide.task_registry import TERMINAL_TASK_STATUSES

from tests.test_platform_e2e import NODES, PLAN, PlatformCase, provider_of

#: (source platform, target platform): every platform is both a source and a target.
LEGS = (
    ("workbuddy", "claude-code"),
    ("claude-code", "codex"),
    ("codex", "workbuddy"),
)
#: background workers exist only on Claude Code in this release.
BACKGROUND_LEGS = (("workbuddy", "claude-code"), ("codex", "claude-code"))
STARTS = ("never_failed", "failed_once", "rerun")
LIVE = NODES[0]  # the node whose worker runs in the "worker running" situation
OTHER = NODES[1]


class StartCase(PlatformCase):
    source = target = None
    target_sdd = False
    #: a background target cannot finish launching without the real `claude --bg`
    background = False

    def begin(self):
        """Run on the source platform with both fixture nodes ready and unclaimed."""
        self.publish(self.source, sdd=False)
        self.assertEqual(sorted(a["issue_id"] for a in self.pending_all("create")), sorted(NODES))

    def pending_all(self, operation):
        return [a for a in self.store.pending() if a["operation"] == operation]

    def attest_target(self, *, background=False):
        self.attest(self.target, sdd=self.target_sdd, background=background)

    def crashing_handoff(self):
        """The run moves, then the disk refuses the node file: a half-done handoff."""
        real = cli_module._atomic_json

        def crash(path, payload):
            if Path(path).name == "nodes.json":
                raise OSError("injected crash before nodes.json")
            return real(path, payload)

        with mock.patch.object(cli_module, "_atomic_json", crash):
            return self.handoff("--adapter", self.target)

    def hand_off(self, start):
        """Issue the handoff as the starting point demands; return the last result."""
        if start == "never_failed":
            return self.handoff("--adapter", self.target)
        if start == "failed_once":
            interrupted = self.crashing_handoff()
            self.assertNotEqual(interrupted.payload.get("status"), "ok", interrupted.payload)
            self.assertEqual(self.snapshot().authorization["agent_id"], self.target)
            return self.handoff("--adapter", self.target)
        first = self.handoff("--adapter", self.target)
        blocked = ("blocked_invalid", "blocked_design", "handoff_refused")
        if not self.background:
            blocked += ("blocked_unknown",)
        self.assertNotIn(first.payload["status"], blocked, first.payload)
        card = self.card_bytes()
        again = self.handoff("--adapter", self.target)
        self.assertEqual(self.card_bytes(), card, "a second handoff must not change the card")
        return again

    def assert_handed_off(self, result, node_ids=NODES):
        self.assertNotIn(result.payload["status"],
                         ("blocked_unknown", "blocked_invalid", "blocked_design", "handoff_refused"),
                         result.payload)
        self.assertEqual(self.snapshot().authorization["agent_id"], self.target)
        self.assertEqual(self.card().agent_id, self.target)
        # The invariants a user sees: exactly one claimable create per waiting node,
        # none of the previous platform left claimable.
        for node_id in node_ids:
            self.assertEqual([a["provider"] for a in self.pending_creates(node_id)],
                             [provider_of(self.target, "visible")], node_id)
            self.assertEqual(self.live_bindings(node_id), [], node_id)
        self.assertEqual([a for a in self.store.pending()
                          if a["provider"] == provider_of(self.source, "visible")], [])

    def topologies(self):
        return {node_id: item["topology"] for node_id, item in self.card_workers().items()}


def _stop_evidence(case, refusal):
    template = dict(refusal.payload["stop_evidence_templates"][0],
                    how_stopped="stopped_on_old_platform", note="旧平台会话已停止（e2e）")
    (case.root / ".vibe" / "handoff-stop.json").write_text(
        json.dumps(template, ensure_ascii=False), encoding="utf-8")
    return case.cli("reconcile", "--plan", PLAN, "--run-id", case.run_id,
                    "--evidence", ".vibe/handoff-stop.json")


def scenario_same_mode(self, start):
    self.begin()
    before = self.topologies()
    self.attest_target()
    result = self.hand_off(start)
    self.assert_handed_off(result)
    self.assertEqual(self.topologies(), before)  # dispatch mode did not change
    self.assertEqual({n: w["mode"] for n, w in self.card_workers().items() if n in NODES},
                     {n: "visible" for n in NODES})


def scenario_visible_to_subagent(self, start):
    self.target_sdd = True
    self.begin()
    self.attest_target()
    result = self.hand_off(start)
    self.assert_handed_off(result)
    workers = self.card_workers()
    for node_id in NODES:
        self.assertEqual(workers[node_id]["topology"], "visible-sdd", node_id)
    # The change is disclosed, once, on the run.
    disclosed = [e for e in load_events(self.paths, self.run_id) if e["event"] == "platform_handoff_disclosed"]
    self.assertTrue(disclosed)
    self.assertEqual(sorted(disclosed[0]["data"]["node_ids"]), sorted(NODES))
    # And it really runs as a subagent session on the new platform, to acceptance.
    for node_id in NODES:
        self.serve(node_id)
    self.assert_card_matches_registry(self.target, NODES)
    for node_id in NODES:
        self.assertEqual(len(self.live_bindings(node_id)), 0)
        self.assertEqual(len(self.bindings(node_id, "developer")), 1)


def scenario_worker_running(self, start):
    self.begin()
    # LIVE's worker is created, located and made visible: it is running.
    for _ in range(3):
        self.answer(LIVE, ("create", "locate", "visibility"))
        self.cli("resume", "--plan", PLAN)
    self.assertEqual(len(self.live_bindings(LIVE)), 1)
    old = self.live_bindings(LIVE)[0]["task_id"]
    self.attest_target()
    card = self.card_bytes()
    refusal = self.handoff("--adapter", self.target)
    self.assertEqual(refusal.payload["status"], "handoff_refused", refusal.payload)
    self.assertEqual(refusal.payload["nodes"], [LIVE])
    self.assertEqual(self.card_bytes(), card)
    self.assertEqual(self.card().agent_id, self.source)
    self.assertEqual(len(self.live_bindings(LIVE)), 1)
    registered = _stop_evidence(self, refusal)
    self.assertNotEqual(registered.payload.get("status"), "blocked_unknown", registered.payload)
    result = self.hand_off(start)
    self.assert_handed_off(result)
    old_binding = [b for b in self.bindings(LIVE) if b["task_id"] == old]
    self.assertEqual([b["status"] for b in old_binding], ["stopped"])
    self.assertIn(old_binding[0]["status"], TERMINAL_TASK_STATUSES)


class Matrix(StartCase):
    """Concrete tests are generated below, one per (leg, situation, start)."""


def _make(leg, scenario, start):
    def test(self):
        self.source, self.target = leg
        scenario(self, start)
    return test


for _leg in LEGS:
    for _name, _scenario in (("same_mode", scenario_same_mode),
                             ("visible_to_subagent", scenario_visible_to_subagent),
                             ("worker_running", scenario_worker_running)):
        for _start in STARTS:
            _method = "test_{}_to_{}__{}__{}".format(_leg[0].replace("-", "_"), _leg[1].replace("-", "_"),
                                                    _name, _start)
            setattr(Matrix, _method, _make(_leg, _scenario, _start))


class BackgroundHandoff(StartCase):
    """Visible becomes background (Claude Code is the only background platform)."""

    def background_handoff(self, leg, start):
        self.source, self.target = leg
        self.background = True
        self.begin()
        self.attest_target(background=True)
        return self.hand_off(start)

    def check_card_and_contract(self, result):
        self.assertEqual(self.card().agent_id, "claude-code")
        workers = self.card_workers()
        nodes = {item["id"]: item for item in json.loads(
            (self.root / ".vibe" / "plans" / PLAN / "nodes.json").read_text(encoding="utf-8"))}
        for node_id in NODES:
            self.assertEqual(workers[node_id]["mode"], "background", node_id)
            self.assertEqual(nodes[node_id]["contract"]["dispatch_topology"], "background", node_id)
            self.assertEqual(nodes[node_id]["contract"]["adapter_id"], "claude-code", node_id)
        # A background worker is not claimed through the mailbox: no create is left
        # for the previous platform, and none is duplicated for the new one.
        for node_id in NODES:
            self.assertLessEqual(len(self.pending_creates(node_id)), 1, node_id)
            self.assertEqual([a for a in self.pending_creates(node_id)
                              if a["provider"] == provider_of("workbuddy", "visible")], [])
            self.assertLessEqual(len(self.live_bindings(node_id)), 1, node_id)

    def exactly_one_worker_each(self):
        for node_id in NODES:
            live = self.live_bindings(node_id)
            self.assertEqual([(b["mode"], b["status"]) for b in live], [("background", "running")], node_id)


def _make_background(leg, start):
    def card_test(self):
        result = self.background_handoff(leg, start)
        self.assertEqual(self.snapshot().authorization["agent_id"], "claude-code")
        self.check_card_and_contract(result)

    # Needs the real `claude --bg` launch (worktree + process): a real-machine
    # item outside this node (PRD platform-e2e: 真机项不在本节点).
    @unittest.expectedFailure
    def worker_test(self):
        self.background_handoff(leg, start)
        self.exactly_one_worker_each()

    return card_test, worker_test


def _make_background_retry(leg):
    # The retry-refusal defect found earlier (a background handoff interrupted
    # after the worker start was recorded was refused on retry) is fixed in
    # monitor_handoff.py (da530f7), so this now passes as a plain test.
    def retry_test(self):
        self.source, self.target = leg
        self.background = True
        self.begin()
        self.attest_target(background=True)
        interrupted = self.crashing_handoff()
        self.assertNotEqual(interrupted.payload.get("status"), "ok", interrupted.payload)
        retry = self.handoff("--adapter", self.target)
        self.assertNotEqual(retry.payload["status"], "handoff_refused", retry.payload)
        self.assertEqual(self.card().agent_id, "claude-code")
        self.check_card_and_contract(retry)

    return retry_test


for _leg in BACKGROUND_LEGS:
    setattr(BackgroundHandoff, "test_background_interrupted_retry_converges__{}_to_{}".format(
        _leg[0].replace("-", "_"), _leg[1].replace("-", "_")), _make_background_retry(_leg))
    for _start in ("never_failed", "rerun"):
        _card, _worker = _make_background(_leg, _start)
        _stem = "{}_to_{}__{}".format(_leg[0].replace("-", "_"), _leg[1].replace("-", "_"), _start)
        setattr(BackgroundHandoff, "test_background_card_and_contract__" + _stem, _card)
        setattr(BackgroundHandoff, "test_background_worker_started__" + _stem, _worker)


if __name__ == "__main__":
    unittest.main()
