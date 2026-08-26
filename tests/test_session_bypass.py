import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
import vibe_guide.session_bypass as session_bypass

from vibe_guide.paths import ProjectPaths
from vibe_guide.session_bypass import (
    BypassError,
    create_challenge,
    consume_bypass,
    grant_bypass,
    is_bypass_valid,
    load_challenge,
    save_challenge,
    end_session,
)


class SessionBypassTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

    def test_challenge_is_transient_and_persistence_is_digest_only(self):
        record = create_challenge("entry-1", self.now)
        self.assertTrue(record.challenge)
        self.assertEqual(len(record.challenge_digest), 64)
        persisted = record.to_dict()
        self.assertNotIn(record.challenge, json.dumps(persisted, sort_keys=True))
        self.assertEqual(record.expires_at, "2026-08-27T09:15:00Z")

        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            save_challenge(paths, record)
            raw = (paths.vibe / "session-bypass.json").read_text(encoding="utf-8")
            self.assertNotIn(record.challenge, raw)
            loaded = load_challenge(paths, "entry-1")
            self.assertEqual(loaded.challenge_digest, record.challenge_digest)
            self.assertIsNone(loaded.challenge)

    def test_consumption_events_never_contain_raw_command(self):
        record = create_challenge("entry-1", self.now)
        result = grant_bypass(record, "BYPASS VIBE " + record.challenge, "unblock", self.now)
        serialized = json.dumps(result.events, sort_keys=True)
        self.assertNotIn(record.challenge, serialized)
        self.assertNotIn("BYPASS VIBE", serialized)

    def test_exact_command_grants_once_and_keeps_wizard_scope(self):
        record = create_challenge("entry-1", self.now)
        result = grant_bypass(
            record, "BYPASS VIBE " + record.challenge, "unblock wizard", self.now
        )
        self.assertTrue(result.granted)
        self.assertEqual(result.record.scope, "wizard")
        self.assertEqual([event["event"] for event in result.events], [
            "session_bypass_granted", "wizard_bypassed"
        ])
        self.assertTrue(is_bypass_valid(result.record, "entry-1", self.now))
        with self.assertRaises(BypassError):
            grant_bypass(
                result.record,
                "BYPASS VIBE " + record.challenge,
                "replay",
                self.now,
            )

    def test_wrong_session_expired_and_non_exact_command_fail_closed(self):
        record = create_challenge("entry-1", self.now)
        with self.assertRaises(BypassError):
            grant_bypass(record, "BYPASS VIBE " + record.challenge, "x", self.now.replace(hour=10))
        with self.assertRaises(BypassError):
            grant_bypass(record, "BYPASS VIBE  " + record.challenge, "x", self.now)
        with self.assertRaises(BypassError):
            grant_bypass(record, "bypass vibe " + record.challenge, "x", self.now)
        result = grant_bypass(record, "BYPASS VIBE " + record.challenge, "x", self.now)
        self.assertFalse(is_bypass_valid(result.record, "other-entry", self.now))
        self.assertFalse(is_bypass_valid(result.record, "entry-1", self.now + timedelta(minutes=16)))

    def test_concurrent_consumers_can_grant_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            command = "BYPASS VIBE " + record.challenge
            barrier = threading.Barrier(2)
            results = []

            def consume():
                barrier.wait()
                try:
                    results.append(("ok", consume_bypass(paths, "entry-1", command, now=self.now)))
                except BypassError:
                    results.append(("error", None))

            workers = [threading.Thread(target=consume) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(sum(status == "ok" for status, _ in results), 1)
            self.assertEqual(sum(status == "error" for status, _ in results), 1)

    def test_stale_record_cannot_reopen_consumed_challenge(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            consume_bypass(paths, "entry-1", "BYPASS VIBE " + record.challenge, now=self.now)
            with self.assertRaises(BypassError):
                save_challenge(paths, record)
            persisted = load_challenge(paths, "entry-1")
            self.assertTrue(persisted.consumed)

    def test_stale_record_cannot_reopen_ended_session(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            end_session(paths, "entry-1")
            with self.assertRaises(BypassError):
                save_challenge(paths, record)
            persisted = load_challenge(paths, "entry-1")
            self.assertTrue(persisted.session_ended)

    def test_session_end_and_consume_race_always_ends_session(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            barrier = threading.Barrier(2)
            outcomes = []

            def end():
                barrier.wait()
                try:
                    end_session(paths, "entry-1")
                    outcomes.append("ended")
                except BypassError:
                    outcomes.append("error")

            def consume():
                barrier.wait()
                try:
                    consume_bypass(paths, "entry-1", "BYPASS VIBE " + record.challenge, now=self.now)
                    outcomes.append("consumed")
                except BypassError:
                    outcomes.append("rejected")

            workers = [threading.Thread(target=end), threading.Thread(target=consume)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            persisted = load_challenge(paths, "entry-1")
            self.assertTrue(persisted.session_ended)
            self.assertFalse(is_bypass_valid(persisted, "entry-1", self.now))
            self.assertNotIn("error", outcomes)

    def test_event_write_failure_leaves_recoverable_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            command = "BYPASS VIBE " + record.challenge
            original_append = session_bypass._append_events

            def fail_after_first_event(event_paths, events):
                original_append(event_paths, events[:1])
                raise OSError("disk")

            with patch("vibe_guide.session_bypass._append_events", side_effect=fail_after_first_event):
                with self.assertRaises(OSError):
                    consume_bypass(paths, "entry-1", command, now=self.now)
            persisted = load_challenge(paths, "entry-1")
            self.assertTrue(persisted.consumed)
            result = consume_bypass(paths, "entry-1", command, now=self.now)
            self.assertTrue(result.granted)
            events = (paths.vibe / "session-events.jsonl").read_text(encoding="utf-8")
            self.assertEqual(events.count("session_bypass_granted"), 1)
            self.assertEqual(events.count("wizard_bypassed"), 1)


if __name__ == "__main__":
    unittest.main()
