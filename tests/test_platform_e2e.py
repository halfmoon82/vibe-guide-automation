"""Three platforms, end to end, through the public CLI and the real provider mailbox.

PRD ``platform-e2e``: one fixture per desktop platform (Claude Code, Codex,
WorkBuddy), judged only by what a user can see -- the authorization card, the
task registry (``tasks.json``) and the provider mailbox.  Nothing here builds a
state a user cannot reach: platforms are attested with ``vibe attest``, plans
are published with ``vibe plan`` / ``vibe authorize``, and a handoff is
``vibe monitor --platform-handoff``.  The shared helpers below are reused by
``tests/test_handoff_e2e_starts.py``.

The user-visible invariants (``assert_user_invariants``):

1. the card's platform and each node's dispatch mode equal what the task
   registry actually holds for that node;
2. a node has at most one non-terminal binding (exactly one while its worker
   runs);
3. a node waiting for a worker has exactly one unclaimed ``create`` in the
   provider mailbox -- never two, never a leftover of the previous platform.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import daemon as daemon_module
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _load_plan, run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_snapshot
from vibe_guide.task_registry import TERMINAL_TASK_STATUSES

from tests import test_integration_review_closeout_entry as closeout

PLAN = "pe"
NODES = ("export-button", "date-range-filter")
CLOSEOUT = "integration-review"
PLATFORMS = ("claude-code", "codex", "workbuddy")
PROBES = (
    "agent", "shell", "subprocess", "worktree", "visible_task.create",
    "visible_task.enter", "visible_task.resume", "visible_task.wait", "in_session_sdd",
)
BACKGROUND_FACT = "background_task.launch"
REQUEST = closeout.REQUEST
FIXTURE = closeout.FIXTURE


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def provider_of(platform, mode):
    manifest = AdapterRegistry().get(platform).manifest
    return manifest["background_provider"] if mode == "background" else manifest["provider"]


class PlatformCase(unittest.TestCase):
    """A fresh project plus the user-level operations a supervisor performs."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pe2e-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = ProjectPaths(self.root)
        self.store = ProviderActionStore(self.paths)
        self.waits = {}
        self.run_id = None
        # The card asks for a detached daemon that would resume the run behind
        # the test's back; the supervisor's own resume calls are the subject here.
        starter = mock.patch.object(daemon_module, "start_daemon",
                                    return_value={"status": "not_started_in_test"})
        starter.start()
        self.addCleanup(starter.stop)

    # -- operations a user can do -------------------------------------------
    def cli(self, *argv):
        return run_cli(list(argv) + ["--json"], self.root)

    def attest(self, platform, *, sdd=True, background=False):
        """``vibe attest`` with every probe true except the dispatch shape asked for."""
        facts = {platform + "." + name: True for name in PROBES}
        facts[platform + ".in_session_sdd"] = sdd
        if background:
            facts[platform + "." + BACKGROUND_FACT] = True
        name = "{}-facts.json".format(platform)
        (self.root / name).write_text(json.dumps(facts), encoding="utf-8")
        result = self.cli("attest", "--adapter", platform, "--facts", name,
                          "--provenance", "platform-e2e", "--project-id", "pe2e")
        self.assertEqual(result.payload["status"], "ok", result.payload)

    def publish(self, platform, *, sdd=True):
        """Init, attest, plan, authorize and start; both fixture nodes are ready."""
        self.assertEqual(self.cli("init", "--confirm").payload["status"], "ok")
        self.attest(platform, sdd=sdd)
        (self.root / "spec.json").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        planned = self.cli("plan", "--request", REQUEST, "--plan-id", PLAN, "--s1", "5,5,5,5,5",
                           "--from-prd", "spec.json", "--adapter", platform)
        self.assertEqual(planned.payload["status"], "ok", planned.payload)
        self.assertEqual(self.cli("authorize", "--plan", PLAN, "--authorize", "AUTHORIZE").payload["status"], "ok")
        started = self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE")
        self.run_id = started.payload["run_id"]

    def answer(self, node_id, operations=None):
        """Answer the node's mailbox requests the way a healthy platform does."""
        for action in self.store.pending():
            if action["issue_id"] == node_id and (operations is None or action["operation"] in operations):
                self.store.complete(
                    action["action_id"],
                    closeout.MailboxClosesTheRunTests.reply(action, self.waits, closeout.CLEARED_CLAIM))

    def serve(self, node_id, rounds=40):
        for _ in range(rounds):
            self.answer(node_id)
            self.cli("resume", "--plan", PLAN)
            if self.snapshot().nodes[node_id]["status"] == "accepted":
                return
        self.fail("node never reached accepted: {} {} {}".format(node_id, self.snapshot().nodes[node_id], [(a["issue_id"], a["operation"]) for a in self.store.pending()]))

    def handoff(self, *extra):
        return self.cli("monitor", "--plan", PLAN, "--authorize", "AUTHORIZE",
                        "--platform-handoff", *extra)

    # -- what a user can read -----------------------------------------------
    def snapshot(self):
        return load_snapshot(self.paths, self.run_id)

    def card(self):
        return _load_plan(self.paths, PLAN)[3]

    def card_bytes(self):
        return (self.root / ".vibe" / "plans" / PLAN / "authorization-card.json").read_bytes()

    def card_workers(self):
        return {item["node_id"]: item for item in self.card().workers}

    def bindings(self, node_id=None, role=None):
        path = self.root / ".vibe" / "runs" / self.run_id / "tasks.json"
        items = _read(path)["bindings"] if path.is_file() else []
        return [item for item in items
                if (node_id is None or item["issue_id"] == node_id)
                and (role is None or item["role"] == role)]

    def live_bindings(self, node_id):
        return [item for item in self.bindings(node_id) if item["status"] not in TERMINAL_TASK_STATUSES]

    def pending_creates(self, node_id):
        return [action for action in self.store.pending()
                if action["issue_id"] == node_id and action["operation"] == "create"]

    # -- the invariants -------------------------------------------------------
    def assert_card_matches_registry(self, platform, node_ids):
        """Invariant 1: the card's platform and mode are the registry's, node by node."""
        card = self.card()
        self.assertEqual(card.agent_id, platform)
        self.assertEqual(self.snapshot().authorization["agent_id"], platform)
        self.assertEqual(_read(self.root / ".vibe" / "plans" / PLAN / "authorization.json")["agent_id"], platform)
        workers = self.card_workers()
        for node_id in node_ids:
            held = self.bindings(node_id, "developer")
            self.assertTrue(held, "no developer binding for " + node_id)
            for binding in held:
                self.assertEqual(binding["mode"], workers[node_id]["mode"], (node_id, binding))
                self.assertEqual(binding["topology"], workers[node_id]["topology"], (node_id, binding))
                self.assertEqual(binding["provider"], provider_of(platform, binding["mode"]), (node_id, binding))

    def assert_awaiting_one_create(self, platform, node_ids):
        """Invariants 2 and 3 for nodes still waiting for a worker on ``platform``."""
        for node_id in node_ids:
            creates = self.pending_creates(node_id)
            self.assertEqual([(a["provider"]) for a in creates],
                             [provider_of(platform, "visible")], (node_id, creates))
            self.assertEqual(self.live_bindings(node_id), [], node_id)
        self.assertEqual(self.card().agent_id, platform)
        workers = self.card_workers()
        for node_id in node_ids:
            self.assertEqual(workers[node_id]["mode"], "visible", node_id)


class EveryPlatformMatchesItsCardTests(PlatformCase):
    """Acceptance 1: a 2-node run per platform, card == task registry."""

    def run_two_nodes(self, platform):
        self.publish(platform)
        self.assertEqual(sorted(a["issue_id"] for a in self.store.pending()), sorted(NODES))
        self.assert_awaiting_one_create(platform, NODES)
        for node_id in NODES:
            self.serve(node_id)
        self.assert_card_matches_registry(platform, NODES)
        for node_id in NODES:
            self.assertEqual(len(self.bindings(node_id, "developer")), 1, node_id)
            self.assertEqual(self.live_bindings(node_id), [], node_id)
            self.assertEqual(self.snapshot().nodes[node_id]["status"], "accepted")

    def test_claude_code(self):
        self.run_two_nodes("claude-code")

    def test_codex(self):
        self.run_two_nodes("codex")

    def test_workbuddy(self):
        self.run_two_nodes("workbuddy")


class FinishedRunRefusesHandoffTests(PlatformCase):
    """Acceptance 3: a run whose every node is accepted keeps its platform."""

    def test_handoff_on_a_fully_accepted_run_is_refused_and_the_card_is_unchanged(self):
        self.publish("workbuddy")
        for node_id in NODES + (CLOSEOUT,):
            self.serve(node_id)
        self.assertEqual(self.snapshot().status, "complete")
        self.attest("claude-code")
        card_before = self.card_bytes()
        files_before = {name: (self.root / ".vibe" / "plans" / PLAN / name).read_bytes()
                        for name in ("nodes.json", "authorization.json")}
        refused = self.handoff("--adapter", "claude-code")
        self.assertEqual(refused.payload["status"], "blocked_invalid", refused.payload)
        self.assertIn("没有未完成节点", refused.payload["reason"])
        self.assertEqual(self.card_bytes(), card_before)
        self.assertEqual({name: (self.root / ".vibe" / "plans" / PLAN / name).read_bytes()
                          for name in files_before}, files_before)
        self.assertEqual(self.card().agent_id, "workbuddy")
        self.assertEqual(self.snapshot().authorization["agent_id"], "workbuddy")


class TestsDriveOnlyThePublicSurfaceTests(unittest.TestCase):
    """Acceptance 4: no direct call of the internal constructors."""

    FORBIDDEN = ("save_" + "contract", "build_" + "contract", "Monitor." + "reauthorize")

    def test_neither_test_file_calls_the_internal_constructors(self):
        here = Path(__file__).resolve().parent
        for name in ("test_platform_e2e.py", "test_handoff_e2e_starts.py"):
            source = (here / name).read_text(encoding="utf-8")
            for word in self.FORBIDDEN:
                self.assertNotIn(word, source, "{} mentions {}".format(name, word))


if __name__ == "__main__":
    unittest.main()
