import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import vibe_guide.state as state_module
from vibe_guide.state import build_preserved_evidence_map, load_evidence_map, preserve_evidence


class V3EvidenceRetentionTests(unittest.TestCase):
    def test_map_hashes_and_copies_historical_files_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            source.mkdir()
            card = source / "authorization-card.json"
            card.write_text(json.dumps({"digest": "old"}), encoding="utf-8")
            before = card.read_bytes()

            evidence = build_preserved_evidence_map(source)
            destination = root / "revision-3"
            self.assertEqual(preserve_evidence(source, destination), evidence)

            self.assertEqual(evidence["authorization-card.json"]["sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(card.read_bytes(), before)
            self.assertEqual(load_evidence_map(destination / "evidence-map.json"), evidence)
            self.assertEqual((destination / "legacy-evidence" / card.name).read_bytes(), before)

    def test_source_symlink_and_destination_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            source.mkdir()
            (source / "real").write_text("x", encoding="utf-8")
            (source / "link").symlink_to(source / "real")
            with self.assertRaises(ValueError):
                build_preserved_evidence_map(source)

            clean_source = root / "clean"
            clean_source.mkdir()
            (clean_source / "x").write_text("x", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision"
            destination.mkdir()
            (destination / "legacy-evidence").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                preserve_evidence(clean_source, destination)

    def test_evidence_map_rejects_absolute_or_parent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence-map.json"
            path.write_text(
                json.dumps({"../outside": {"sha256": "a" * 64}}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_evidence_map(path)

    def test_directory_swap_during_copy_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "x").write_bytes(b"payload")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision"
            original_replace = os.replace

            def swap_then_replace(src, dst, **kwargs):
                nested = destination / "legacy-evidence" / "nested"
                if nested.is_dir() and not nested.is_symlink():
                    nested.rename(destination / "legacy-evidence" / "detached")
                    nested.symlink_to(outside, target_is_directory=True)
                return original_replace(src, dst, **kwargs)

            with mock.patch("os.replace", side_effect=swap_then_replace):
                preserve_evidence(source, destination)

            self.assertEqual(list(outside.iterdir()), [])

    def test_root_swap_after_guard_cannot_create_outside_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            source.mkdir()
            (source / "x").write_bytes(b"payload")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision"
            destination.mkdir()
            original_guard = state_module._assert_evidence_destination
            swapped = False

            def guard_then_swap(destination_root, path=None):
                nonlocal swapped
                result = original_guard(destination_root, path)
                if path is None and not swapped:
                    swapped = True
                    destination.rename(root / "detached")
                    destination.symlink_to(outside, target_is_directory=True)
                return result

            with mock.patch.object(state_module, "_assert_evidence_destination", side_effect=guard_then_swap):
                with self.assertRaises((ValueError, OSError)):
                    preserve_evidence(source, destination)

            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_swap_before_copy_cannot_write_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "x").write_bytes(b"payload")
            outside = root / "outside"
            outside.mkdir()
            destination = root / "revision"
            original_guard = state_module._assert_evidence_destination
            swapped = False

            def guard_then_swap(destination_root, path=None):
                nonlocal swapped
                result = original_guard(destination_root, path)
                if path is not None and Path(path).name == "nested" and not swapped:
                    swapped = True
                    nested = destination / "legacy-evidence" / "nested"
                    if nested.exists() and not nested.is_symlink():
                        nested.rename(destination / "legacy-evidence" / "detached")
                        nested.symlink_to(outside, target_is_directory=True)
                return result

            with mock.patch.object(state_module, "_assert_evidence_destination", side_effect=guard_then_swap):
                with self.assertRaises((ValueError, OSError)):
                    preserve_evidence(source, destination)

            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
