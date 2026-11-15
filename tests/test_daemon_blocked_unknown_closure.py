"""P0b (A1/A11): a blocked_unknown run is never closed out just because a
grace period ran out.  Only a real boundary handed to the product manager
(decision card) lets the run park; an engineering "unresolved" keeps being served.
"""
import unittest
from types import SimpleNamespace

from vibe_guide import daemon as dm
from vibe_guide.supervisor import Supervisor

from tests.test_claude_bg_delivery_gate import BackgroundDeliveryGateTests as Base


class BlockedUnknownClosure(Base):
    for _name in [n for n in dir(Base) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def make_daemon(self, snapshot):
        supervisor = Supervisor(self.paths, self.monitor(), self.runner(), snapshot.run_id)
        config = dm.DaemonConfig(interval=0, blocked_grace=0, heal_interval=10 ** 9)
        daemon = dm.Daemon(self.paths, snapshot.run_id, supervisor, config,
                           sleep=lambda s: None, notifier=lambda *a, **k: {"status": "sent"},
                           assess=lambda: {"status": "ok", "nodes": {}},
                           archive=lambda: None, sessions=lambda s: {"live": [], "unknown": None})
        daemon._snapshot = SimpleNamespace(nodes={"bg": {"status": "blocked_unknown"}})
        daemon._run_status = "blocked_unknown"
        daemon._parked_since = daemon.clock() - 10 ** 6  # far past blocked_grace
        return daemon

    def test_grace_expiry_alone_does_not_park(self):
        daemon = self.make_daemon(self.start())
        self.assertFalse(daemon._parked())
        self.assertIsNone(daemon._closeout_status())

    def test_engineering_unresolved_does_not_park(self):
        daemon = self.make_daemon(self.start())
        daemon._unresolved("bg", "self_heal_no_progress", "修了几轮仍无进展")
        self.assertFalse(daemon._parked())

    def test_real_boundary_escalation_parks(self):
        daemon = self.make_daemon(self.start())
        daemon._escalate("bg", "needs_decision", "需要新增依赖", "new_dependency")
        self.assertTrue(daemon._parked())

    def test_real_boundary_after_engineering_unresolved_parks(self):
        daemon = self.make_daemon(self.start())
        daemon._unresolved("bg", "self_heal_no_progress", "修了几轮仍无进展")
        daemon._escalate("bg", "needs_decision", "需要新增依赖", "new_dependency")
        node = daemon._heal_state()["nodes"]["bg"]
        self.assertFalse(node["engineering"])
        self.assertTrue(daemon._parked())

    def test_unserved_blocked_node_keeps_alerting(self):
        daemon = self.make_daemon(self.start())
        daemon._parked()
        self.assertIn("bg", daemon._heal_state()["unresolved"])


del Base
if __name__ == "__main__":
    unittest.main()
