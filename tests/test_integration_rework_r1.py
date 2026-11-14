"""Independent read-only-candidate integration probes; all runtime writes are temporary."""
import hashlib
import contextlib
import copy
import json
import sys
import unittest
from unittest.mock import patch

from tests.support_vibe_home import isolated_environment
from tests import test_monitor as monitor_fixture
from vibe_guide import protocol_adoption
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.runners.provider_action import ProviderActionRunner, validate_result_shape
from vibe_guide.state import load_events
from vibe_guide.monitor import Monitor
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.digests import canonical_json_digest


class IntegrationProbes(unittest.TestCase):
    def test_r6_handoff_records_cannot_hide_behind_filename(self):
        for adopted in (False, True):
            for reverse in (False, True):
                for variant in ('canonical_duplicate', 'arbitrary_duplicate',
                                'renamed_unique', 'missing', 'event_digest'):
                    with self.subTest(adopted=adopted, reverse=reverse, variant=variant):
                        fixture, monitor, runner, snapshot, path, handoff, dispatch_path, role = self.r5_retry_fixture(adopted, reverse)
                        if variant in ('canonical_duplicate', 'arbitrary_duplicate'):
                            extra = path.parent / ('n1-extra-' + role + '-' +
                                str(handoff['intent']['generation']) + '.json'
                                if variant == 'canonical_duplicate' else 'duplicate.json')
                            changed = copy.deepcopy(handoff)
                            changed['intent']['owner'] = {'foreign': True}
                            changed['intent']['digest'] = canonical_json_digest(
                                {k: v for k, v in changed['intent'].items() if k != 'digest'})
                            ProviderActionStore._atomic(extra, changed)
                        elif variant == 'renamed_unique':
                            path.rename(path.parent / 'duplicate.json')
                        elif variant == 'missing':
                            path.unlink()
                        else:
                            # A well-digested handoff no longer agrees with
                            # its original committed event; keep the event.
                            handoff['intent']['extra_evidence'] = 'changed'
                            handoff['intent']['digest'] = canonical_json_digest(
                                {k: v for k, v in handoff['intent'].items() if k != 'digest'})
                            ProviderActionStore._atomic(path, handoff)
                        frozen = {p.name: p.read_bytes() for p in path.parent.glob('*.json')}
                        leases = {p.name: p.read_bytes() for p in (fixture.paths.vibe / 'leases').glob('*.json')}
                        dispatch = dispatch_path.read_bytes()
                        before = len(runner.start_calls)
                        fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                        try:
                            with patch('vibe_guide.monitor.time.time', return_value=1e12):
                                fresh.tick(snapshot.run_id, runner)
                        except (PermissionError, ValueError, RuntimeError):
                            pass
                        self.assertEqual(len(runner.start_calls), before)
                        self.assertEqual(leases, {p.name: p.read_bytes() for p in (fixture.paths.vibe / 'leases').glob('*.json')})
                        self.assertEqual(dispatch, dispatch_path.read_bytes())
                        self.assertEqual(frozen, {p.name: p.read_bytes() for p in path.parent.glob('*.json')})

    def r5_retry_fixture(self, adopted, reverse):
        probe = IntegrationProbes()
        self.addCleanup(probe.doCleanups)
        fixture, monitor, runner, snapshot = probe.r4_retired_fixture(adopted)
        snapshot = monitor.tick(snapshot.run_id, runner)
        runner.events[('n1', 'developer')] = [('delivered', {'evidence': 'delivery'})]
        role = 'reviewer'
        if reverse:
            snapshot = monitor.tick(snapshot.run_id, runner)
            runner.events[('n1', 'reviewer')] = [('review_finding', {
                'finding': 'ordinary P1', 'in_contract': True})]
            role = 'developer'
        with patch.object(runner, 'start', return_value={
                'kind': 'retryable', 'reason': 'temporary capacity'}) as attempt:
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertGreaterEqual(attempt.call_count, 1)
        current = snapshot.nodes['n1']
        self.assertEqual(current['status'], 'retry_pending')
        self.assertTrue(current['retryable_action']['same_task'])
        self.assertEqual(current['start_intent']['role'], role)
        directory = fixture.paths.vibe / 'runs' / snapshot.run_id
        paths = [p for p in (directory / 'writer-handoffs').glob('*.json')
                 if ProviderActionStore._read(p)['intent']['role'] == role]
        self.assertEqual(len(paths), 1)
        path = paths[0]
        handoff = ProviderActionStore._read(path)
        self.assertEqual(handoff['phase'], 'committed')
        dispatch_path = directory / 'start-dispatches' / (current['start_intent']['intent_id'] + '.json')
        dispatch = ProviderActionStore._read(dispatch_path)
        self.assertEqual(dispatch['intent'], current['start_intent'])
        self.assertEqual(dispatch['digest'], canonical_json_digest(
            {k: v for k, v in dispatch.items() if k != 'digest'}))
        return fixture, monitor, runner, snapshot, path, handoff, dispatch_path, role

    def r5_check_handoff(self, variants, expect_refused):
        for adopted in (False, True):
            for reverse in (False, True):
                for variant in variants:
                    with self.subTest(adopted=adopted, reverse=reverse, variant=variant):
                        fixture, monitor, runner, snapshot, path, handoff, dispatch_path, role = self.r5_retry_fixture(adopted, reverse)
                        intent = handoff['intent']
                        source = 'reviewer' if reverse else 'developer'
                        if variant == 'digest': intent['digest'] = 'f' * 64
                        elif variant == 'owner': intent['owner'] = {'foreign': True}
                        elif variant == 'source_task': intent[source]['task_id'] = 'foreign-task'
                        elif variant == 'intent': intent['intent_digest'] = 'f' * 64
                        elif variant == 'auth': intent['authorization_digest'] = 'f' * 64
                        elif variant == 'contract': intent['node_contract_digest'] = 'f' * 64
                        elif variant == 'generation': intent['generation'] += 9
                        elif variant == 'role': intent['role'] = source
                        elif variant == 'phase': handoff['phase'] = 'prepared'
                        if variant not in ('digest', 'safe'):
                            intent['digest'] = canonical_json_digest(
                                {k: v for k, v in intent.items() if k != 'digest'})
                        if variant != 'safe': ProviderActionStore._atomic(path, handoff)
                        leases = {p.name: p.read_bytes() for p in (fixture.paths.vibe / 'leases').glob('*.json')}
                        before = len(runner.start_calls)
                        dispatch_bytes = dispatch_path.read_bytes()
                        fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                        refused = None
                        try:
                            # Advance only the retry deadline; do not mutate
                            # snapshot, lease, bindings, or dispatch evidence.
                            control = (patch.object(Monitor, '_handoff_delivery_writer', self.control_method)
                                if hasattr(self, 'control_method') else contextlib.nullcontext())
                            with control, patch('vibe_guide.monitor.time.time', return_value=1e12):
                                result = fresh.tick(snapshot.run_id, runner)
                        except (PermissionError, ValueError, RuntimeError) as error:
                            result = None
                            refused = str(error)
                        after = len(runner.start_calls)
                        self.assertEqual(leases, {p.name: p.read_bytes() for p in (fixture.paths.vibe / 'leases').glob('*.json')})
                        print('HANDOFF_RETRY', json.dumps({
                            'adopted': adopted, 'reverse': reverse, 'variant': variant,
                            'run_id': snapshot.run_id, 'role': role, 'starts_before': before,
                            'starts_after': after, 'result': None if result is None else result.nodes['n1']['status'],
                            'error': refused, 'lease_unchanged': True,
                            'dispatch_unchanged': dispatch_bytes == dispatch_path.read_bytes()}), flush=True)
                        if expect_refused:
                            self.assertEqual(after, before, 'invalid handoff must reject before retry start')
                        else:
                            self.assertIsNotNone(result)
                            self.assertEqual(after, before + 1)
                            self.assertEqual(result.nodes['n1']['active_task']['role'], role)
                            for _ in range(2):
                                result = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values())).tick(snapshot.run_id, runner)
                            self.assertEqual(len(runner.start_calls), after)

    def test_r5_safe_role_retry_is_not_blocked(self):
        self.r5_check_handoff(('safe',), expect_refused=False)

    def test_r5_changed_handoff_cannot_borrow_valid_dispatch(self):
        self.r5_check_handoff(('digest', 'owner', 'source_task', 'intent', 'auth',
                            'contract', 'generation', 'role', 'phase'), expect_refused=True)


    def r4_retired_fixture(self, adopted=False, crash=False):
        from tests import test_model_fallback_retry as fallback
        from vibe_guide.adapters.task_provider import ProviderUnavailable, ProviderActionStore
        from vibe_guide.models import Plan, AgentCapabilities
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import load_snapshot
        fixture = fallback.ModelFallbackDispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        node = fallback._fallback_node("n1", fallbacks=("k2",))
        monitor = Monitor(fixture.paths, Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft"), [node])
        caps = AgentCapabilities("fake", True, True, True, True, True, "full")
        old = authorize(build_authorization_card(monitor.plan, [node], caps), "AUTHORIZE")
        runner = fallback._FlakyRunner([ProviderUnavailable("quota exhausted for k1"),
                                        ProviderUnavailable("quota exhausted for k2")])
        if adopted:
            with patch.object(monitor, "_context_allows_dispatch", return_value=False):
                snapshot = monitor.start(old, runner)
            approval = fixture.paths.root / "approval.md"
            approval.write_text("fixture explicit complete r4 adoption")
            manifest = protocol_adoption.responsibility_manifest()
            receipt = protocol_adoption.record_adoption(fixture.paths, plan_id=snapshot.plan_id,
                run_id=snapshot.run_id, manifest=manifest, accepted_protocols=manifest["protocols"],
                evidence_ref="approval.md", evidence_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
                authorization_digest=old.digest, contract_digest=old.node_contract_digest)
            card = build_authorization_card(monitor.plan, [node], caps,
                ruleset_digest=manifest["digest"], adoption_digest=receipt["adoption_digest"])
            snapshot = monitor.reauthorize(snapshot.run_id, authorize(card, "AUTHORIZE"), runner,
                                            "executable_contract_changed")
            self.assertEqual(protocol_adoption.effective_responsibility(fixture.paths, snapshot)["status"], "effective")
        else:
            snapshot = monitor.start(old, runner)
        if crash:
            atomic = ProviderActionStore._atomic
            def stop(path, payload):
                result = atomic(path, payload)
                if path.parent.name == "model-retirements" and payload.get("phase") == "committed":
                    raise KeyboardInterrupt("committed before final snapshot")
                return result
            with patch.object(ProviderActionStore, "_atomic", side_effect=stop):
                with self.assertRaises(KeyboardInterrupt):
                    monitor.tick(snapshot.run_id, runner)
        else:
            snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = load_snapshot(fixture.paths, snapshot.run_id)
        return fixture, monitor, runner, snapshot

    def test_r4_full_retired_role_lifecycle(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import retirement_writer_lease
        for adopted in (False, True):
            for crash in (False, True):
                with self.subTest(adopted=adopted, crash=crash):
                    fixture, monitor, runner, snapshot = self.r4_retired_fixture(adopted, crash)
                    def tick():
                        fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                        return fresh.tick(snapshot.run_id, runner)
                    snapshot = tick()
                    self.assertEqual(snapshot.nodes["n1"]["status"], "running", snapshot.nodes["n1"])
                    for _ in range(2):
                        snapshot = tick()
                    starts = len(runner.start_calls)
                    identities = {}
                    for role, event, data, status in (
                        ("developer", "delivered", {"evidence": "delivery"}, "review"),
                        ("reviewer", "review_finding", {"finding": "ordinary P1", "in_contract": True}, "rework"),
                        ("developer", "delivered", {"evidence": "rework delivery"}, "review"),
                        ("reviewer", "accepted", {"evidence": "independent clearance"}, "accepted")):
                        active = snapshot.nodes["n1"]["active_task"]
                        identities.setdefault(role, active["task_id"])
                        self.assertEqual(identities[role], active["task_id"])
                        runner.events[("n1", role)] = [(event, data)]
                        snapshot = tick()
                        self.assertEqual(snapshot.nodes["n1"]["status"], status, snapshot.nodes["n1"])
                        before = len(runner.start_calls)
                        for _ in range(2):
                            snapshot = tick()
                        self.assertEqual(len(runner.start_calls), before)
                        leases = list((fixture.paths.vibe / "leases").glob("*.json"))
                        self.assertLessEqual(len(leases), 1)
                        active = snapshot.nodes["n1"].get("active_task")
                        if active:
                            lease = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", snapshot.run_id)
                            self.assertEqual(lease["record"]["role"], active["role"])
                            self.assertEqual(lease["record"]["generation"], active["generation"])
                    self.assertEqual(len(runner.start_calls), starts + 3)


    def test_r4_interrupted_handoff_drift_preserves_writer(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.digests import canonical_json_digest
        for variant in ("digest", "owner", "intent", "task", "auth"):
            with self.subTest(variant=variant):
                fixture, monitor, runner, snapshot = self.r4_retired_fixture(True)
                snapshot = monitor.tick(snapshot.run_id, runner)
                runner.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
                atomic = ProviderActionStore._atomic
                def stop(path, payload):
                    result = atomic(path, payload)
                    if path.parent.name == "writer-handoffs" and payload.get("phase") == "committed":
                        raise KeyboardInterrupt("committed handoff")
                    return result
                with patch.object(ProviderActionStore, "_atomic", side_effect=stop):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor.tick(snapshot.run_id, runner)
                path, = (fixture.paths.vibe / "runs" / snapshot.run_id / "writer-handoffs").glob("*.json")
                record = ProviderActionStore._read(path)
                intent = record["intent"]
                if variant == "digest":
                    intent["digest"] = "f" * 64
                else:
                    if variant == "owner": intent["owner"] = {"foreign": True}
                    elif variant == "intent": intent["intent_digest"] = "f" * 64
                    elif variant == "task": intent["developer"]["task_id"] = "foreign"
                    else: intent["authorization_digest"] = "f" * 64
                    intent["digest"] = canonical_json_digest({k: v for k, v in intent.items() if k != "digest"})
                ProviderActionStore._atomic(path, record)
                before = {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")}
                starts = len(runner.start_calls)
                fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                with self.assertRaises((PermissionError, ValueError, RuntimeError)):
                    fresh.tick(snapshot.run_id, runner)
                self.assertEqual(len(runner.start_calls), starts)
                self.assertEqual(before, {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")})

    def test_r4_reverse_handoff_interruptions(self):
        self.check_r4_handoff_interruptions(reverse=True)

    def test_r4_role_handoff_interruptions(self):
        self.check_r4_handoff_interruptions()

    def check_r4_handoff_interruptions(self, reverse=False):
        from vibe_guide.monitor import Monitor
        from vibe_guide.adapters.task_provider import ProviderActionStore
        for adopted in (False, True):
            for phase in ("prepared", "committed"):
                with self.subTest(adopted=adopted, phase=phase):
                    fixture, monitor, runner, snapshot = self.r4_retired_fixture(adopted)
                    snapshot = monitor.tick(snapshot.run_id, runner)
                    runner.events[("n1", "developer")] = [("delivered", {"evidence": "delivery"})]
                    if reverse:
                        snapshot = monitor.tick(snapshot.run_id, runner)
                        runner.events[("n1", "reviewer")] = [("review_finding", {
                            "finding": "ordinary P1", "in_contract": True})]
                    atomic = ProviderActionStore._atomic
                    hit = []
                    def stop(path, payload):
                        result = atomic(path, payload)
                        if path.parent.name == "writer-handoffs" and payload.get("phase") == phase and not hit:
                            hit.append(True)
                            raise KeyboardInterrupt(phase)
                        return result
                    with patch.object(ProviderActionStore, "_atomic", side_effect=stop):
                        with self.assertRaises(KeyboardInterrupt):
                            monitor.tick(snapshot.run_id, runner)
                    self.assertTrue(hit)
                    for _ in range(3):
                        fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                        snapshot = fresh.tick(snapshot.run_id, runner)
                    self.assertEqual(snapshot.nodes["n1"]["status"], "rework" if reverse else "review", snapshot.nodes["n1"])
                    self.assertEqual([c["role"] for c in runner.start_calls][-2:],
                                     ["reviewer", "developer"] if reverse else ["developer", "reviewer"])
                    self.assertEqual(len(list((fixture.paths.vibe / "leases").glob("*.json"))), 1)

    def test_r3_committed_retirement_next_writer_is_fail_closed(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.state import acquire_writer_lease, load_snapshot
        from vibe_guide.supervisor import SupervisorLease
        from tests.test_model_fallback_retry import _FlakyRunner
        for variant in ("next-role", "next-generation", "foreign", "takeover", "safe"):
            with self.subTest(variant=variant):
                fixture, monitor, args = self.capture_failed_dispatch()
                atomic = ProviderActionStore._atomic
                def stop(path, payload):
                    result = atomic(path, payload)
                    if path.parent.name == "model-retirements" and payload.get("phase") == "committed":
                        raise KeyboardInterrupt("committed before final snapshot")
                    return result
                with patch.object(ProviderActionStore, "_atomic", side_effect=stop):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor._mark_model_unavailable(*args)
                snapshot = load_snapshot(fixture.paths, args[0].run_id)
                if variant != "safe":
                    self.assertTrue(acquire_writer_lease(fixture.paths,
                        "other" if variant == "foreign" else "n1", ".worktrees/n1",
                        "run-foreign" if variant == "foreign" else snapshot.run_id,
                        "e" * 64, "developer" if variant == "next-generation" else "reviewer",
                        3 if variant == "next-generation" else 2))
                if variant == "takeover":
                    SupervisorLease(fixture.paths).takeover(snapshot.run_id, 0,
                        owner={"session_id": "new-owner", "provider": "codex"})
                before = {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")}
                runner = _FlakyRunner([None])
                for _ in range(2):
                    fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                    if variant != "safe":
                        with self.assertRaises((PermissionError, ValueError, RuntimeError)):
                            fresh.tick(snapshot.run_id, runner)
                        self.assertEqual(runner.start_calls, [])
                        self.assertEqual(before, {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")})
                    else:
                        result = fresh.tick(snapshot.run_id, runner)
                        self.assertEqual(result.nodes["n1"]["status"], "running")
                        self.assertEqual(len(runner.start_calls), 1)
                        self.assertEqual(len(list((fixture.paths.vibe / "leases").glob("*.json"))), 1)

    def test_r2_prepared_retirement_refuses_changed_recovery(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import load_snapshot, acquire_writer_lease, release_writer_lease, retirement_writer_lease
        from vibe_guide.supervisor import SupervisorLease
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from tests.test_model_fallback_retry import _FlakyRunner
        for variant in ("owner", "role", "generation", "intent", "pending", "inflight", "foreign", "next"):
            with self.subTest(variant=variant):
                fixture, monitor, args = self.capture_failed_dispatch()
                with patch("vibe_guide.monitor.release_writer_lease", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor._mark_model_unavailable(*args)
                snapshot = load_snapshot(fixture.paths, args[0].run_id)
                current = snapshot.nodes["n1"]
                if variant == "owner":
                    SupervisorLease(fixture.paths).takeover(snapshot.run_id, 0,
                        owner={"session_id": "new-owner", "provider": "codex"})
                elif variant in ("role", "generation", "intent"):
                    current["start_intent"] = dict(args[2].get("start_intent") or {},
                        role="developer", generation=1, intent_digest="a" * 64)
                    if variant == "role":
                        current["active_role"] = "reviewer"
                    elif variant == "generation":
                        current["developer_generation"] = 8
                elif variant == "pending":
                    with patch("vibe_guide.adapters.task_provider.require_entry"):
                        ProviderActionStore(fixture.paths).request(operation="wait", provider="codex-app-visible",
                            run_id=snapshot.run_id, issue_id="n1", role="developer", generation=1,
                            native_tool="wait_threads", request={})
                elif variant == "inflight":
                    snapshot.handles["n1"] = "inflight"
                elif variant == "foreign":
                    acquire_writer_lease(fixture.paths, "foreign", ".worktrees/n1", "run-foreign",
                        "d" * 64, "reviewer", 1)
                elif variant == "next":
                    writer = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", snapshot.run_id)
                    release_writer_lease(fixture.paths, "n1", ".worktrees/n1", snapshot.run_id, expected=writer)
                    acquire_writer_lease(fixture.paths, "n1", ".worktrees/n1", snapshot.run_id,
                        "e" * 64, "reviewer", 2)
                monitor_fixture.save_snapshot(fixture.paths, snapshot)
                before = {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")}
                fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                runner = _FlakyRunner([None])
                with self.assertRaises((PermissionError, ValueError, RuntimeError)):
                    fresh.tick(snapshot.run_id, runner)
                self.assertEqual(runner.start_calls, [])
                self.assertEqual(before, {p.name: p.read_bytes() for p in (fixture.paths.vibe / "leases").glob("*.json")})

    def test_r2_native_business_matrix(self):
        for sdd in (False, True):
            for variant in ("valid", "missing", "tampered", "stale"):
                with self.subTest(sdd=sdd, variant=variant):
                    self.check_business_transport(variant=variant, native=True, sdd=sdd, replay=True)

    def test_r2_retirement_crash_boundaries_recover_once(self):
        from vibe_guide.monitor import Monitor
        from vibe_guide.state import load_snapshot, retirement_writer_lease
        from tests.test_model_fallback_retry import _FlakyRunner
        from vibe_guide.adapters.task_provider import ProviderActionStore
        for boundary in ("save_before", "save_after", "release_before", "release_after",
                         "event_before", "event_after", "commit_before", "commit_after"):
            with self.subTest(boundary=boundary):
                fixture, monitor, args = self.capture_failed_dispatch()
                original_save = monitor._save_snapshot
                original_record = monitor._record
                import vibe_guide.monitor as module
                original_release = module.release_writer_lease
                original_atomic = ProviderActionStore._atomic
                hit = []
                def interrupt(name, call, *a, **kw):
                    if name + "_before" == boundary and not hit:
                        hit.append(True)
                        raise KeyboardInterrupt(boundary)
                    result = call(*a, **kw)
                    if name + "_after" == boundary and not hit:
                        hit.append(True)
                        raise KeyboardInterrupt(boundary)
                    return result
                def atomic(path, payload):
                    if "model-retirements" in str(path) and payload.get("phase") == "committed":
                        return interrupt("commit", original_atomic, path, payload)
                    return original_atomic(path, payload)
                def record(snapshot, name, data, *a):
                    if name == "model_writer_retired":
                        return interrupt("event", original_record, snapshot, name, data, *a)
                    return original_record(snapshot, name, data, *a)
                with patch.object(monitor, "_save_snapshot", side_effect=lambda *a: interrupt("save", original_save, *a)), \
                     patch.object(monitor, "_record", side_effect=record), \
                     patch.object(module, "release_writer_lease", side_effect=lambda *a, **kw: interrupt("release", original_release, *a, **kw)), \
                     patch.object(ProviderActionStore, "_atomic", side_effect=atomic):
                    with self.assertRaises(KeyboardInterrupt):
                        monitor._mark_model_unavailable(*args)
                self.assertTrue(hit)
                fresh = Monitor(fixture.paths, monitor.plan, list(monitor.nodes.values()))
                runner = _FlakyRunner([None])
                restored = fresh.tick(args[0].run_id, runner)
                self.assertEqual(restored.nodes["n1"]["status"], "running")
                self.assertEqual(len(runner.start_calls), 1)
                writer = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id)
                self.assertEqual(writer["record"]["generation"], 2)
                fresh.tick(args[0].run_id, runner)
                self.assertEqual(len(runner.start_calls), 1)
                self.assertEqual(retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id), writer)

    def capture_failed_dispatch(self):
        from tests import test_model_fallback_retry as fallback
        from vibe_guide.adapters.task_provider import ProviderUnavailable
        fixture = fallback.ModelFallbackDispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        monitor, runner, snapshot = fixture._start_fallback_run(
            fallback._fallback_node("n1", fallbacks=("k2",)),
            [ProviderUnavailable("quota exhausted for model k1"),
             ProviderUnavailable("quota exhausted for model k2")])
        captured = []
        def stop(*args):
            captured.extend(args)
            raise KeyboardInterrupt("freeze before retirement")
        with patch.object(monitor, "_mark_model_unavailable", side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                monitor.tick(snapshot.run_id, runner)
        return fixture, monitor, captured

    def test_failed_snapshot_save_preserves_writer(self):
        from vibe_guide.state import retirement_writer_lease
        fixture, monitor, args = self.capture_failed_dispatch()
        before = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id)
        with patch.object(monitor, "_save_snapshot", side_effect=RuntimeError("crash before save")):
            with self.assertRaises(RuntimeError):
                monitor._mark_model_unavailable(*args)
        self.assertEqual(retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id), before)

    def test_role_generation_and_intent_drift_preserve_writer(self):
        from vibe_guide.state import retirement_writer_lease
        from vibe_guide.supervisor import SupervisorLeaseConflict
        for key, value in (("role", "reviewer"), ("generation", 9), ("intent_digest", "f" * 64)):
            with self.subTest(key=key):
                fixture, monitor, args = self.capture_failed_dispatch()
                before = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id)
                args[2]["start_intent"][key] = value
                with self.assertRaises(SupervisorLeaseConflict):
                    monitor._mark_model_unavailable(*args)
                self.assertEqual(retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id), before)

    def test_inflight_handle_preserves_writer(self):
        from vibe_guide.state import retirement_writer_lease
        from vibe_guide.supervisor import SupervisorLeaseConflict
        fixture, monitor, args = self.capture_failed_dispatch()
        before = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id)
        args[0].handles["n1"] = "inflight-handle"
        with self.assertRaises(SupervisorLeaseConflict):
            monitor._mark_model_unavailable(*args)
        self.assertEqual(retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id), before)

    def test_duplicate_retirement_cannot_delete_next_writer(self):
        from vibe_guide.state import retirement_writer_lease, acquire_writer_lease
        from vibe_guide.supervisor import SupervisorLeaseConflict
        fixture, monitor, args = self.capture_failed_dispatch()
        monitor._mark_model_unavailable(*args)
        self.assertTrue(acquire_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id,
            "e" * 64, "reviewer", 2))
        before = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id)
        with self.assertRaises(SupervisorLeaseConflict):
            monitor._mark_model_unavailable(*args)
        self.assertEqual(retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", args[0].run_id), before)

    def test_exhaustion_after_owner_takeover_preserves_writer(self):
        from tests import test_model_fallback_retry as fallback
        from vibe_guide.state import retirement_writer_lease
        from vibe_guide.supervisor import SupervisorLease, SupervisorLeaseConflict
        from vibe_guide.adapters.task_provider import ProviderUnavailable
        fixture = fallback.ModelFallbackDispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        current = fallback._fallback_node("n1", fallbacks=("k2",))
        from vibe_guide.monitor import Monitor
        from vibe_guide.models import Plan, AgentCapabilities
        plan = Plan("plan-1", 1, "docs/prd.md", [current.id], "draft")
        monitor = Monitor(fixture.paths, plan, [current])
        authorization = authorize(build_authorization_card(plan, [current],
            AgentCapabilities("fake", True, True, True, True, True, "full")), "AUTHORIZE")
        runner = fallback._FlakyRunner([ProviderUnavailable("quota exhausted for model k1"),
                                         ProviderUnavailable("quota exhausted for model k2")])
        with patch.object(monitor, "_context_allows_dispatch", return_value=False):
            snapshot = monitor.start(authorization, runner)
        old_owner = SupervisorLease(fixture.paths)
        old_epoch = old_owner.takeover(snapshot.run_id, 0,
            owner={"session_id": "old-owner", "provider": "codex"})
        monitor.lease_guard = lambda: old_owner.ensure_current(snapshot.run_id)
        snapshot = monitor.tick(snapshot.run_id, runner)
        fallback_plan = monitor._plan_model_fallback
        evidence = {}
        def race(snapshot, node_id, contract, *args, **kwargs):
            run_id = snapshot.run_id
            evidence["run_id"] = run_id
            evidence["before"] = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", run_id)
            evidence["epoch"] = SupervisorLease(fixture.paths).takeover(run_id, old_epoch,
                owner={"session_id": "new-owner", "provider": "codex"})
            return fallback_plan(snapshot, node_id, contract, *args, **kwargs)
        try:
            with patch.object(monitor, "_plan_model_fallback", side_effect=race):
                result = monitor.tick(snapshot.run_id, runner)
                if not evidence:
                    print("NO_EXHAUSTION_REACHED", json.dumps(result.nodes), flush=True)
        except (PermissionError, RuntimeError, SupervisorLeaseConflict) as error:
            print("OWNER_REJECT", str(error), flush=True)
        self.assertIsNotNone(evidence["before"])
        after = retirement_writer_lease(fixture.paths, "n1", ".worktrees/n1", evidence["run_id"])
        print("OWNER_LEASE_BEFORE_AFTER", json.dumps({"epoch": evidence["epoch"],
              "before": evidence["before"], "after": after}), flush=True)
        self.assertEqual(after, evidence["before"], "old dispatch released writer after owner takeover")

    def test_visible_worker_business_proof_reaches_monitor(self):
        self.check_business_transport()

    def test_missing_business_proof_is_refused(self):
        self.check_business_transport(variant="missing")

    def test_tampered_business_proof_is_refused(self):
        self.check_business_transport(variant="tampered")

    def test_stale_round_business_proof_is_refused(self):
        self.check_business_transport(variant="stale")

    def test_native_raw_business_proof_projection_keeps_acceptance(self):
        self.check_business_transport(native=True)

    def test_business_transport_snapshot_loss(self):
        self.check_business_transport(native=True, replay=True)

    def test_visible_sdd_business_transport(self):
        self.check_business_transport(native=True, sdd=True)

    def check_business_transport(self, variant="valid", native=False, replay=False, sdd=False):
        fixture = monitor_fixture.MonitorTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        upstream = monitor_fixture.node("up")
        upstream.contract.pop("worker_profile")
        upstream.contract.update(project_id="fixture-project", adapter_id="claude-code",
                                 acceptance_examples=["required material"])
        downstream = monitor_fixture.node("down", ["up"])
        downstream.parallel_group = "g2"
        downstream.contract.pop("worker_profile")
        downstream.contract.update(project_id="fixture-project", adapter_id="claude-code")
        nodes = [upstream, downstream]
        monitor, old = fixture.authorized_monitor(nodes)
        if sdd:
            monitor._topology_rulings = {"claude-code": "in_session_sdd"}
            old = authorize(build_authorization_card(monitor.plan, nodes, fixture.capabilities,
                topology_rulings=monitor._topology_rulings), "AUTHORIZE")
        with patch("vibe_guide.adapters.task_provider.require_entry"):
            runner = ProviderActionRunner(fixture.paths, "claude-code", "claude-code-visible")
            request = runner.store.request

            def transport(**kwargs):
                action = request(**kwargs)
                responses = {
                    "create": {"binding": {"task_id": kwargs["role"] + "-" + kwargs["issue_id"], "host": "local"}},
                    "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True},
                }
                if kwargs["operation"] in responses:
                    runner.store.complete(action["action_id"], responses[kwargs["operation"]],
                                          request_digest=action["request_digest"])
                return action

            with patch.object(runner.store, "request", side_effect=transport):
                # Hold the initial old-rule dispatch only; reauthorization and
                # all evaluated r4 dispatch/poll/acceptance gates are unpatched.
                with patch.object(monitor, "_context_allows_dispatch", return_value=False):
                    snapshot = monitor.start(old, runner)
                approval = fixture.paths.root / "approval.md"
                approval.write_text("explicit complete r4 adoption")
                manifest = protocol_adoption.responsibility_manifest()
                receipt = protocol_adoption.record_adoption(
                    fixture.paths, plan_id=snapshot.plan_id, run_id=snapshot.run_id,
                    manifest=manifest, accepted_protocols=manifest["protocols"],
                    evidence_ref="approval.md", evidence_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
                    authorization_digest=old.digest, contract_digest=old.node_contract_digest)
                card = build_authorization_card(monitor.plan, nodes, fixture.capabilities,
                    topology_rulings=monitor._topology_rulings,
                    ruleset_digest=manifest["digest"], adoption_digest=receipt["adoption_digest"])
                snapshot = monitor.reauthorize(snapshot.run_id, authorize(card, "AUTHORIZE"),
                                                runner, "executable_contract_changed")
                self.assertEqual(protocol_adoption.effective_responsibility(fixture.paths, snapshot)["status"], "effective")
                snapshot = monitor.tick(snapshot.run_id, runner)
                wait, = [a for a in runner.store.pending(snapshot.run_id) if a["operation"] == "wait"]
                if not sdd:
                    runner.store.complete(wait["action_id"], {"status": "complete", "event": "delivered",
                        "evidence": "developer delivery", "cursor": "developer-complete"},
                        request_digest=wait["request_digest"])
                    snapshot = monitor.tick(snapshot.run_id, runner)
                    self.assertEqual(snapshot.nodes["up"]["status"], "review")
                    snapshot = monitor.tick(snapshot.run_id, runner)
                material = fixture.paths.root / "material.md"
                material.write_text("independently checked fixture material")
                proof = {"round": 1, "inputs": {}, "items": {"required material": {
                    "status": "satisfied", "evidence_ref": "material.md",
                    "evidence_sha256": hashlib.sha256(material.read_bytes()).hexdigest()}}}
                payload = {"event": "accepted", "evidence": "review clear", "business_acceptance": proof,
                    "delivery_evidence": {"completion_marker": "DONE", "delivery_path": "material.md",
                                           "thread_status": "complete"}}
                if variant == "missing":
                    payload.pop("business_acceptance")
                elif variant == "tampered":
                    material.write_text("modified after independent review")
                elif variant == "stale":
                    proof["round"] = 0
                role = "reviewer"
                generation = snapshot.nodes["up"]["review_generation"]
                if sdd:
                    from tests.test_issue_83_validate_first import _good_review
                    role = "developer"
                    generation = snapshot.nodes["up"]["developer_generation"]
                    payload.update(event="complete", in_session_review=_good_review())
                print("C4_EFFECTIVE", receipt["adoption_digest"], flush=True)
                wait, = [a for a in runner.store.pending(snapshot.run_id)
                          if a["operation"] == "wait" and a["role"] == role]
                preserved = copy.deepcopy(snapshot)
                monitor_fixture.save_snapshot(fixture.paths, preserved)
                if native:
                    from vibe_guide.runners.provider_action import complete_native_mailbox_action
                    complete_native_mailbox_action(fixture.paths, snapshot.run_id, wait["action_id"],
                        {"structuredContent": dict(payload, status="complete")},
                        request_digest=wait["request_digest"])
                else:
                    runner.store.record_worker_delivery(snapshot.run_id, "up", role, payload, generation)
                result = runner.store.result(wait["action_id"])
                if variant != "missing":
                    self.assertEqual(result["business_acceptance"], proof)
                print("STORED_RESULT_HAS_PROOF", True, flush=True)
                # Freeze downstream scheduling to isolate acceptance replay
                # from a new provider start's unrelated identity migration.
                with patch.object(monitor, "_context_allows_dispatch", return_value=False):
                    snapshot = monitor.tick(snapshot.run_id, runner)
                print("FINAL_STATE", json.dumps({k: snapshot.nodes["up"].get(k) for k in
                    ("status", "business_acceptance", "reason", "business_acceptance_pending")}), flush=True)
                print("RELEVANT_EVENTS", json.dumps([e for e in load_events(fixture.paths, snapshot.run_id)
                    if "business" in e["event"]]), flush=True)
                if variant == "valid":
                    self.assertEqual(snapshot.nodes["up"]["status"], "accepted")
                    self.assertEqual(snapshot.nodes["up"]["business_acceptance"], proof)
                else:
                    self.assertNotEqual(snapshot.nodes["up"]["status"], "accepted")
                if replay:
                    monitor_fixture.save_snapshot(fixture.paths, preserved)
                    with patch.object(monitor, "_context_allows_dispatch", return_value=False):
                        snapshot = monitor.tick(snapshot.run_id, runner)
                    if variant == "valid":
                        self.assertEqual(snapshot.nodes["up"]["status"], "accepted")
                        self.assertEqual(snapshot.nodes["up"]["business_acceptance"], proof)
                    else:
                        self.assertNotEqual(snapshot.nodes["up"]["status"], "accepted")

    def test_native_wait_shape_supports_required_business_proof(self):
        payload = {"status": "complete", "event": "accepted", "evidence": "clear",
                   "business_acceptance": {"round": 1, "inputs": {}, "items": {}}}
        validate_result_shape("wait", payload, role="reviewer")
