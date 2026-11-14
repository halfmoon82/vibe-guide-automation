"""A native side effect survives interruption before its mailbox projection."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import complete_native_mailbox_action
from tests import support_vibe_home as hermetic

setUpModule, tearDownModule = hermetic.module_hooks()


class NativeReceiptReconciliationTests(unittest.TestCase):
    def native_wait(self, structured, *, envelope=None, request=None, interrupt=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(temporary.name)))
        action = self.store.request(operation='wait', provider='codex-app-visible',
            run_id='run-1', issue_id='n1', role='developer', generation=1,
            sequence=0,
            native_tool='codex_app__wait_threads', request=request or {
                'targets': [{'threadId': 'original', 'hostId': 'local', 'afterCursor': 'prior:opaque'}], 'timeoutMs': 0})
        response = envelope if envelope is not None else {'content': [{'type': 'text', 'text': json.dumps(structured)}], 'isError': False}
        atomic = ProviderActionStore._atomic
        def interrupted(path, record):
            if path.parent.name == 'results':
                raise OSError('NB1 fixture projection interruption')
            return atomic(path, record)
        if interrupt:
            with patch.object(ProviderActionStore, '_atomic', new=staticmethod(interrupted)):
                with self.assertRaisesRegex(OSError, 'projection interruption'):
                    complete_native_mailbox_action(self.store.paths, 'run-1', action['action_id'], response,
                        request_digest=action['request_digest'])
        else:
            complete_native_mailbox_action(self.store.paths, 'run-1', action['action_id'], response,
                request_digest=action['request_digest'])
        raw = self.store._native_receipt_path(action['action_id'])
        self.assertEqual(json.loads(raw.read_text())['native_response'], response)
        return action, response, ProviderActionStore(self.store.paths).result(action['action_id'])

    def native_poll_fixture(self):
        # Public schema fixture only, not a provider-authenticated App receipt.
        return {'timedOut': False, 'polls': [{'schemaVersion': 1, 'cursor': 'opaque:position/35',
            'thread': {'id': 'original', 'hostId': 'local', 'status': {'type': 'idle'}},
            'latestTurn': {'id': 'turn-1', 'status': 'completed'},
            'latestAssistantMessage': {'text': 'done'}}]}

    def test_nb1_native_poll_retains_exact_cursor_without_business_event(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                body = self.native_poll_fixture()
                body['timedOut'] = timeout
                _, _, result = self.native_wait(body)
                self.assertEqual(result['cursor'], 'opaque:position/35')
                self.assertEqual(result['status'], 'timeout' if timeout else 'unknown')
                self.assertNotIn('event', result)
                self.assertNotIn('latestAssistantMessage', result['polls'][0])
                self.assertEqual(result['polls'][0]['thread'], {'id': 'original', 'hostId': 'local'})

    def test_nb1_wrong_missing_duplicate_error_or_partial_poll_never_moves_cursor(self):
        import copy
        base = self.native_poll_fixture()
        variants = []
        for key, value in (('id', 'foreign'), ('hostId', 'foreign'), ('hostId', None)):
            body = copy.deepcopy(base); body['polls'][0]['thread'][key] = value; variants.append(body)
        for key, value in (('polls', []), ('polls', base['polls'] * 2), ('errors', [{'threadId': 'original', 'hostId': 'local', 'error': 'timeout'}]),
                           ('errors', [{'threadId': 'foreign', 'error': 'denied'}]), ('errors', {}),
                           ('partial', True), ('hasMore', True), ('nextCursor', 'more'), ('timedOut', 'false')):
            variants.append(dict(base, **{key: value}))
        for cursor in (None, '', 5):
            body = copy.deepcopy(base); body['polls'][0]['cursor'] = cursor; variants.append(body)
        for body in variants:
            with self.subTest(body=body):
                _, _, result = self.native_wait(body)
                self.assertEqual(result['status'], 'unknown')
                self.assertNotIn('cursor', result)
                self.assertNotIn('polls', result)
                self.assertTrue(result['reason'].startswith('native_wait:'))

    def test_nb1_envelope_error_conflict_and_multi_target_are_not_poll_success(self):
        body = self.native_poll_fixture()
        import copy
        other = copy.deepcopy(body); other['polls'][0]['cursor'] = 'different'
        envelopes = [dict(structuredContent=body, isError=True),
            dict(structuredContent=body, content=[{'type': 'text', 'text': json.dumps(other)}])]
        for envelope in envelopes:
            _, _, result = self.native_wait(body, envelope=envelope)
            self.assertEqual(result['status'], 'unknown')
            self.assertNotIn('cursor', result)
        _, _, result = self.native_wait(body, request={'targets': [
            {'threadId': 'original', 'hostId': 'local'}, {'threadId': 'foreign', 'hostId': 'local'}]})
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('cursor', result)

    def test_nb1_poll_raw_interruption_replay_and_conflict_preserve_first_observation(self):
        action, response, result = self.native_wait(self.native_poll_fixture(), interrupt=True)
        raw = self.store._native_receipt_path(action['action_id'])
        frozen = raw.read_bytes()
        self.assertEqual(result['cursor'], 'opaque:position/35')
        complete_native_mailbox_action(self.store.paths, 'run-1', action['action_id'], response,
            request_digest=action['request_digest'])
        changed = self.native_poll_fixture(); changed['polls'][0]['cursor'] = 'late:other'
        self.assertFalse(complete_native_mailbox_action(self.store.paths, 'run-1', action['action_id'],
            {'structuredContent': changed}, request_digest=action['request_digest']))
        self.assertEqual(raw.read_bytes(), frozen)
        self.assertEqual(self.store.result(action['action_id']), result)
        self.assertEqual(len(list(self.store._directory('observations').glob('*.json'))), 1)

    def test_nb1_first_unknown_cannot_be_overwritten_by_late_good_poll(self):
        action, _, first = self.native_wait({'polls': []})
        raw = self.store._native_receipt_path(action['action_id']).read_bytes()
        complete_native_mailbox_action(self.store.paths, 'run-1', action['action_id'],
            {'structuredContent': self.native_poll_fixture()}, request_digest=action['request_digest'])
        self.assertEqual(self.store.result(action['action_id']), first)
        self.assertEqual(self.store._native_receipt_path(action['action_id']).read_bytes(), raw)

    def test_nb1_bare_navigation_visibility_and_send_explain_missing_proof(self):
        for operation, reason in (
            ('locate', 'native_locate:target_navigation_evidence_missing'),
            ('visibility', 'native_visibility:visible_direct_enter_capability_evidence_missing'),
            ('resume', 'native_resume:acceptance_not_execution_evidence')):
            with self.subTest(operation=operation):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store = ProviderActionStore(ProjectPaths(Path(temporary.name)))
                action = store.request(operation=operation, provider='codex-app-visible', run_id='run-1',
                    issue_id='n1', role='developer', generation=1, native_tool='native_' + operation, request={})
                response = {'structuredContent': {'threadId': 'original', 'hostId': 'local', 'status': 'accepted'}}
                complete_native_mailbox_action(store.paths, 'run-1', action['action_id'], response,
                    request_digest=action['request_digest'])
                self.assertEqual(store.result(action['action_id']), {'status': 'unknown', 'reason': reason})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(self.tmp.name)))
        self.action = self.store.request(operation="create", provider="codex-app-visible",
            run_id="run-1", issue_id="n1", role="developer", generation=1,
            native_tool="create_thread", request={})
        self.action_id = self.action["action_id"]

    def raw_path(self):
        return self.store.root / "native-receipts" / (
            self.action_id + "-" + self.action["request_digest"] + ".json")

    def interrupt_projection(self, payload):
        atomic = self.store._atomic
        def interrupted(path, record):
            if path.parent.name == "results":
                raise OSError("synthetic interruption before projection")
            return atomic(path, record)
        with patch.object(self.store, "_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before projection"):
                self.store.complete(self.action_id, payload)

    def test_raw_is_durable_before_projection_and_reconciles_without_new_create(self):
        first = {"threadId": "original", "hostId": "local"}
        self.interrupt_projection(first)
        self.assertTrue(self.raw_path().is_file())
        frozen = self.raw_path().read_bytes()
        fresh = ProviderActionStore(self.store.paths)
        self.assertEqual(fresh.pending("run-1"), [])
        self.assertEqual(fresh.result(self.action_id), first)
        self.assertEqual(self.raw_path().read_bytes(), frozen)
        self.assertEqual(fresh.pending("run-1"), [])
        self.assertEqual(len(list((self.store.root / "requests").glob("*.json"))), 1)

    def test_conflicting_callback_after_interruption_cannot_replace_first_raw(self):
        first = {"failed": True, "reason": "native failure", "wake_failed": True}
        self.interrupt_projection(first)
        frozen = self.raw_path().read_bytes()
        self.assertFalse(self.store.complete(self.action_id, {"resumed": True}))
        self.assertEqual(self.store.result(self.action_id), first)
        self.assertEqual(self.raw_path().read_bytes(), frozen)
        self.assertEqual(len(list((self.store.root / "observations").glob("*.json"))), 1)

    def test_modified_raw_does_not_rebuild_a_projection(self):
        self.interrupt_projection({"threadId": "original", "hostId": "local"})
        raw = json.loads(self.raw_path().read_text())
        raw["payload"]["threadId"] = "forged"
        self.raw_path().write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "native receipt"):
            self.store.result(self.action_id)
        self.assertFalse((self.store.root / "results" / (self.action_id + ".json")).exists())

    def test_worker_report_is_not_a_native_receipt(self):
        self.store.complete(self.action_id, {"event": "blocked_engineering"}, native=False)
        self.assertFalse(self.raw_path().exists())

    def test_unanswered_legacy_request_remains_pending_without_completion_lock(self):
        legacy = dict(self.action, action_id="action-legacy", request_digest="b" * 64)
        self.store._atomic(self.store.root / "requests" / "action-legacy.json", legacy)
        self.assertIsNone(self.store.result("action-legacy"))
        self.assertIn(legacy, self.store.pending("run-1"))
        self.assertFalse((self.store.root / "locks" / "action-legacy.lock").exists())

    def test_legacy_request_cannot_authenticate_a_native_receipt(self):
        legacy = dict(self.action, action_id="action-legacy", request_digest="b" * 64)
        self.store._atomic(self.store.root / "requests" / "action-legacy.json", legacy)
        self.store._atomic(self.store._directory("native-receipts") /
                          ("action-legacy-" + "b" * 64 + ".json"), {})
        with self.assertRaisesRegex(ValueError, "request identity"):
            self.store.result("action-legacy")

    def test_public_native_adapter_preserves_full_envelope(self):
        response = {"content": [{"type": "text", "text": "created"}],
                    "structuredContent": {"threadId": "original", "hostId": "local",
                                          "status": "ready"}, "isError": False}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        raw = json.loads(self.raw_path().read_text())
        self.assertEqual(raw["native_response"], response)
        self.assertEqual(self.store.result(self.action_id),
                         {"binding": {"threadId": "original", "hostId": "local"}})

    def test_setup_pending_does_not_bind_client_thread_as_ready_task(self):
        response = {"structuredContent": {"clientThreadId": "pending-client",
                                          "status": "pending"}}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        result = self.store.result(self.action_id)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("binding", result)

    def test_json_text_tool_content_projects_ready_binding_and_keeps_raw(self):
        response = {"content": [{"type": "text", "text": json.dumps(
            {"threadId": "original", "hostId": "local", "status": "ready"})}]}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        self.assertEqual(self.store.result(self.action_id),
                         {"binding": {"threadId": "original", "hostId": "local"}})
        self.assertEqual(json.loads(self.raw_path().read_text())["native_response"], response)

    def test_unknown_create_state_cannot_become_ready_by_having_ids(self):
        response = {"structuredContent": {"threadId": "original", "hostId": "local",
                                           "status": "unknown_timeout"}}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        self.assertNotIn("binding", self.store.result(self.action_id))

    def test_documented_ready_create_pair_needs_no_undocumented_status(self):
        # create_thread's advertised contract identifies ready by threadId +
        # hostId, pending by clientThreadId; it does not require status.
        response = {"structuredContent": {"threadId": "original", "hostId": "local"}}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        self.assertEqual(self.store.result(self.action_id),
                         {"binding": {"threadId": "original", "hostId": "local"}})

    def test_disagreeing_text_and_structured_content_stays_unknown(self):
        response = {"structuredContent": {"threadId": "original", "hostId": "local", "status": "ready"},
                    "content": [{"type": "text", "text": json.dumps(
                        {"threadId": "different", "hostId": "local", "status": "ready"})}]}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        self.assertNotIn("binding", self.store.result(self.action_id))

    def test_error_envelope_and_conflicting_native_observation_are_preserved(self):
        response = {"isError": True, "content": [{"type": "text", "text": "capacity"}]}
        complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            response, request_digest=self.action["request_digest"])
        self.assertNotIn("binding", self.store.result(self.action_id))
        changed = dict(response, content=[{"type": "text", "text": "different error"}])
        self.assertFalse(complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
            changed, request_digest=self.action["request_digest"]))
        observation = json.loads(next((self.store.root / "observations").glob("*.json")).read_text())
        self.assertEqual(observation["native_response"], changed)

    def test_projection_cannot_drift_from_its_native_receipt(self):
        self.store.complete(self.action_id, {"failed": True, "reason": "native failure"})
        path = self.store.root / "results" / (self.action_id + ".json")
        record = json.loads(path.read_text())
        record["payload"] = {"resumed": True}
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "native receipt"):
            self.store.result(self.action_id)

    def test_cli_native_envelope_keeps_conflicting_callback_observation(self):
        from vibe_guide.cli import run_cli
        response = {"structuredContent": {"threadId": "original", "hostId": "local", "status": "ready"}}
        source = self.store.paths.root / "response.json"
        source.write_text(json.dumps(response))
        args = ["supervisor-mailbox", "--run-id", "run-1", "--complete", self.action_id,
                "--result", str(source), "--native-response", "--request-digest",
                self.action["request_digest"], "--json"]
        result = run_cli(args, cwd=self.store.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(json.loads(self.raw_path().read_text())["native_response"], response)
        changed = {"isError": True, "content": [{"type": "text", "text": "different failure"}]}
        source.write_text(json.dumps(changed))
        result = run_cli(args, cwd=self.store.paths.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        observations = list((self.store.root / "observations").glob("*.json"))
        self.assertEqual(len(observations), 1)
        self.assertEqual(json.loads(observations[0].read_text())["native_response"], changed)

    def test_full_native_envelope_survives_projection_crash(self):
        response = {"structuredContent": {"threadId": "original", "hostId": "local", "status": "ready"},
                    "content": [{"type": "text", "text": "native creation receipt"}]}
        atomic = ProviderActionStore._atomic
        def interrupted(path, record):
            if path.parent.name == "results":
                raise OSError("interrupted native projection")
            return atomic(path, record)
        with patch.object(ProviderActionStore, "_atomic", new=staticmethod(interrupted)):
            with self.assertRaisesRegex(OSError, "native projection"):
                complete_native_mailbox_action(self.store.paths, "run-1", self.action_id,
                    response, request_digest=self.action["request_digest"])
        frozen = self.raw_path().read_bytes()
        self.assertEqual(json.loads(frozen)["native_response"], response)
        fresh = ProviderActionStore(self.store.paths)
        self.assertEqual(fresh.result(self.action_id),
                         {"binding": {"threadId": "original", "hostId": "local"}})
        self.assertEqual(self.raw_path().read_bytes(), frozen)
        self.assertEqual(fresh.pending("run-1"), [])

    def test_all_lifecycle_operations_preserve_exact_explicit_safe_result_fields(self):
        examples = {"resume": {"resumed": True}, "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True},
                    "wait": {"status": "timeout", "cursor": "native-cursor"}}
        for operation, expected in examples.items():
            with self.subTest(operation=operation):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store = ProviderActionStore(ProjectPaths(Path(temporary.name)))
                action = store.request(operation=operation, provider="codex-app-visible",
                    run_id="run-1", issue_id="n1", role="developer", generation=1,
                    native_tool="native_" + operation, request={})
                response = {"structuredContent": dict(expected, transport_detail="keep raw")}
                complete_native_mailbox_action(store.paths, "run-1", action["action_id"],
                    response, request_digest=action["request_digest"])
                self.assertEqual(store.result(action["action_id"]), expected)

    def test_queued_resume_cannot_invent_resumed_flag_or_recovery_success(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ProviderActionStore(ProjectPaths(Path(temporary.name)))
        action = store.request(operation="resume", provider="codex-app-visible",
            run_id="run-1", issue_id="n1", role="developer", generation=1,
            native_tool="send_message_to_thread", request={})
        complete_native_mailbox_action(store.paths, "run-1", action["action_id"],
            {"structuredContent": {"status": "queued", "threadId": "original"}},
            request_digest=action["request_digest"])
        self.assertEqual(store.result(action["action_id"]), {"status": "unknown",
            "reason": "native_resume:acceptance_not_execution_evidence"})
