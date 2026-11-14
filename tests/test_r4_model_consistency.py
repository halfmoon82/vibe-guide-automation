"""Real Monitor lifecycle in an isolated fixture; no real user run is used."""
import json
import os
import hashlib
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from vibe_guide import protocols
from vibe_guide.authorization import (
    AuthorizationRecord, authorize, build_authorization_card,
    refresh_authorization_card, is_authorization_valid,
)
from vibe_guide.models import Plan
from vibe_guide.initializer import init_project
from vibe_guide.protocols import PRD_GUIDE_PROPOSAL_RELATIVE, load_protocol
from tests.test_integrated_prd_acceptance import FULL, V42_STATE, _ProjectCase
from tests.test_model_tiers import _node


class ModelConsistencyTests(_ProjectCase):
    state = V42_STATE

    def test_different_developer_reviewer_tiers_match_actual_dispatch(self):
        home = Path(os.environ["VIBE_HOME"])
        (home / "config.json").write_text(json.dumps({"model_map": {"claude-code": {
            "light": ["manual-writer"], "standard": ["manual-reviewer"]
        }}}))
        node = _node("docs", files=["docs/a.md"], adapter_id="claude-code")
        monitor, runner, snapshot = self._start([node], {
            ("docs", "developer"): [("complete", {"evidence": "delivery"})],
            ("docs", "reviewer"): [("accepted", {"evidence": "review"})],
        })
        self._tick_until(monitor, runner, snapshot, "docs", {"accepted"}, limit=10)
        self.assertEqual(
            [(c["role"], c["worker_profile"]["model"]) for c in runner.start_calls],
            [("developer", "manual-writer"), ("reviewer", "manual-reviewer")],
        )

    def test_reinitialization_does_not_rewrite_an_existing_binding(self):
        node = _node("docs", files=["docs/a.md"], adapter_id="claude-code")
        monitor, runner, snapshot = self._start([node], {})
        monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(runner.start_calls), 1)
        tasks = self.root / ".vibe/runs" / snapshot.run_id / "tasks.json"
        before = tasks.read_bytes()
        home = Path(os.environ["VIBE_HOME"])
        (home / "config.json").write_text('{"model_map":{"claude-code":{"light":["new-default"]}}}')
        init_project(self.paths, True)
        self.assertEqual(tasks.read_bytes(), before)
        self.assertEqual(runner.start_calls[0]["worker_profile"]["model"], "haiku")


class ResponsibilityAdoptionIntegrationTests(_ProjectCase):
    """C4's external implementation is fixed; real initializer controls writes."""
    def setUp(self):
        super().setUp()
        init_project(self.paths, True)
        self.copy = self.root / PRD_GUIDE_PROPOSAL_RELATIVE
        self.old = '# old shipped responsibility contract\n'
        self.copy.write_text(self.old)
        self.manifest = {
            'schema': 1, 'version': 'r4',
            'protocols': {
                'vibe_guide/protocols/' + name + '.md': hashlib.sha256(load_protocol(name).encode()).hexdigest()
                for name in ('prd-guide', 'vibe-entry', 'supervisor-card', 'heal-session', 'visible-sdd-worker')
            },
        }
        self.manifest['digest'] = hashlib.sha256(json.dumps(
            self.manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode()).hexdigest()
        self.recorded = []
        self.interface = types.ModuleType('vibe_guide.protocol_adoption')
        self.interface.responsibility_manifest = lambda: self.manifest
        self.interface.record_adoption = self.record
        fixture = patch.dict('sys.modules', {'vibe_guide.protocol_adoption': self.interface})
        fixture.start()
        self.addCleanup(fixture.stop)
        old_digest = hashlib.sha256(self.old.encode()).hexdigest()
        registered = patch.dict(protocols.SHIPPED_PROTOCOL_DIGESTS, {
            'prd-guide': protocols.SHIPPED_PROTOCOL_DIGESTS['prd-guide'] | {old_digest},
        })
        registered.start()
        self.addCleanup(registered.stop)

    def record(self, paths, **payload):
        self.recorded.append(payload)
        complete = payload['accepted_protocols'] == self.manifest['protocols']
        status = 'pending_reauthorization' if complete else ('partial' if payload['accepted_protocols'] else 'rejected')
        return {'status': status,
                'adoption_digest': 'fixture-adoption'}

    def test_old_unedited_responsibility_copy_is_not_silently_refreshed(self):
        result = init_project(self.paths, True)
        self.assertEqual(self.copy.read_text(), self.old)
        self.assertTrue(any('采纳' in note for note in result.notes), result.notes)
        self.assertEqual(self.recorded, [], 'init confirmation is not ruleset acceptance')

    def test_explicit_partial_or_complete_adoption_is_forwarded_but_not_applied(self):
        partial = {key: value for key, value in self.manifest['protocols'].items() if key.endswith('/prd-guide.md')}
        for accepted, expected in (({}, 'rejected'), (partial, 'partial'), (self.manifest['protocols'], 'pending_reauthorization')):
            context = {
                'plan_id': 'fixture-plan', 'run_id': 'fixture-run',
                'accepted_protocols': accepted, 'evidence_ref': 'docs/approved-rules.md',
                'evidence_sha256': 'fixture-approved-sha',
                'authorization_digest': 'fixture-authorization', 'contract_digest': 'fixture-contract',
            }
            result = init_project(self.paths, True, responsibility_adoption=context)
            self.assertEqual(self.recorded[-1], dict(context, manifest=self.manifest))
            self.assertEqual(result.responsibility['adoption']['status'], expected)
            self.assertEqual(self.copy.read_text(), self.old)

    def test_unreadable_c4_is_not_permission_to_refresh(self):
        def broken():
            raise ValueError('untrusted ruleset')
        self.interface.responsibility_manifest = broken
        result = init_project(self.paths, True)
        self.assertEqual(self.copy.read_text(), self.old)
        self.assertTrue(any('unknown' in note for note in result.notes), result.notes)

    def test_incomplete_or_invalid_c4_manifest_preserves_both_old_protocol_copies(self):
        entry = self.root / '.vibe/proposals/skills/vibe-entry/SKILL.md'
        old_entry = '# old shipped entry responsibility contract\n'
        old_entry_digest = hashlib.sha256(old_entry.encode()).hexdigest()
        bodies = [('empty', dict(self.manifest, protocols={}))]
        for path in self.manifest['protocols']:
            bodies.append(('missing:' + path, dict(self.manifest, protocols={
                key: value for key, value in self.manifest['protocols'].items() if key != path
            })))
        bodies.extend([
            ('invalid-schema', dict(self.manifest, schema=True)),
            ('invalid-version', dict(self.manifest, version='r5')),
            ('missing-version', {key: value for key, value in self.manifest.items() if key != 'version'}),
            ('invalid-protocol-sha', dict(self.manifest, protocols=dict(
                self.manifest['protocols'], **{'vibe_guide/protocols/prd-guide.md': 'not-a-sha'}
            ))),
            ('unpublished-protocol-sha', dict(self.manifest, protocols=dict(
                self.manifest['protocols'], **{'vibe_guide/protocols/prd-guide.md': 'f' * 64}
            ))),
        ])
        cases = []
        for label, body in bodies:
            body = {key: value for key, value in body.items() if key != 'digest'}
            cases.append((label, dict(body, digest=hashlib.sha256(json.dumps(
                body, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest())))
        cases.extend([
            ('invalid-digest', dict(self.manifest, digest='not-a-sha')),
            ('mismatched-digest', dict(self.manifest, digest='0' * 64)),
            ('uppercase-digest', dict(self.manifest, digest=self.manifest['digest'].upper())),
            ('missing-digest', {key: value for key, value in self.manifest.items() if key != 'digest'}),
        ])
        with patch.dict(protocols.SHIPPED_PROTOCOL_DIGESTS, {
            'vibe-entry': protocols.SHIPPED_PROTOCOL_DIGESTS['vibe-entry'] | {old_entry_digest},
        }):
            for label, manifest in cases:
                with self.subTest(manifest=label):
                    self.copy.write_text(self.old)
                    entry.write_text(old_entry)
                    self.interface.responsibility_manifest = lambda: manifest
                    result = init_project(self.paths, True)
                    self.assertEqual((
                        result.responsibility['status'], self.copy.read_text(), entry.read_text(),
                    ), ('unknown', self.old, old_entry))
                    self.assertEqual(self.recorded, [])


class ResponsibilityAuthorizationTests(unittest.TestCase):
    def test_ruleset_and_adoption_are_signed_and_survive_serialization_and_refresh(self):
        node = _node('n', adapter_id='claude-code')
        plan = Plan('p', 1, 'docs/prd.md', ['n'], 'draft')
        before = build_authorization_card(plan, [node], FULL)
        card = build_authorization_card(
            plan, [node], FULL, ruleset_digest='a' * 64, adoption_digest='b' * 64,
            responsibility={'status': 'pending_reauthorization'},
        )
        self.assertNotEqual(before.digest, card.digest)
        self.assertEqual(card.to_dict()['responsibility']['status'], 'pending_reauthorization')
        record = AuthorizationRecord.from_dict(authorize(card, 'AUTHORIZE').to_dict())
        self.assertEqual((record.ruleset_digest, record.adoption_digest), ('a' * 64, 'b' * 64))
        self.assertTrue(is_authorization_valid(record, plan, [node]))
        self.assertFalse(is_authorization_valid(replace(record, ruleset_digest='c' * 64), plan, [node]))
        refreshed = refresh_authorization_card(plan, [node], card)
        self.assertEqual((refreshed.ruleset_digest, refreshed.adoption_digest), ('a' * 64, 'b' * 64))
        self.assertTrue(is_authorization_valid(authorize(refreshed, 'AUTHORIZE'), plan, [node]))
        invalid = record.to_dict()
        invalid.pop('adoption_digest')
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(invalid)

    def test_partial_binding_is_refused_and_old_cards_have_no_new_binding_fields(self):
        node = _node('n', adapter_id='claude-code')
        plan = Plan('p', 1, 'docs/prd.md', ['n'], 'draft')
        before = build_authorization_card(plan, [node], FULL)
        self.assertNotIn('ruleset_digest', before.to_dict())
        self.assertNotIn('adoption_digest', authorize(before, 'AUTHORIZE').to_dict())
        with self.assertRaises(ValueError):
            build_authorization_card(plan, [node], FULL, ruleset_digest='a' * 64)
