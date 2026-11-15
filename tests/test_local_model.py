"""P2a: Ollama product initialization — probe, init flow, fallback, cache.

These tests exercise the contract only; they never install Ollama, download a
model, or touch system configuration.  Network is simulated by an injected
``fetch``/``post`` callable.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from vibe_guide import local_model


def _fetch_ok(payload=None, status=200):
    def fetch(url, timeout):
        return status, json.dumps(payload if payload is not None else {"models": [{"name": "qwen2.5:7b"}]})
    return fetch


def _fetch_refused(url, timeout):
    raise OSError("connection refused")


def _fetch_timeout(url, timeout):
    raise TimeoutError("timed out")


def _fetch_bad_json(url, timeout):
    return 200, "not json {"


def _post_ok(payload=None):
    def post(url, body, timeout):
        return 200, json.dumps(payload if payload is not None else {"response": "ok"})
    return post


def _post_busy(url, body, timeout):
    return 503, "{}"


class ProbeTests(unittest.TestCase):
    def test_local_available_lists_models(self):
        result = local_model.probe_ollama(endpoint="http://127.0.0.1:11434", fetch=_fetch_ok())
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["local"])
        self.assertEqual(result["models"], ["qwen2.5:7b"])

    def test_http_200_with_invalid_json_is_not_ready(self):
        # Contract: HTTP 200 alone must not count as ready.
        result = local_model.probe_ollama(endpoint="http://127.0.0.1:11434", fetch=_fetch_bad_json)
        self.assertEqual(result["status"], "unknown")
        self.assertNotEqual(result["status"], "available")

    def test_timeout_and_refused_have_distinct_status(self):
        timeout = local_model.probe_ollama(endpoint="http://127.0.0.1:11434", fetch=_fetch_timeout)
        refused = local_model.probe_ollama(endpoint="http://127.0.0.1:11434", fetch=_fetch_refused)
        self.assertEqual(timeout["status"], "unknown_timeout")
        self.assertEqual(refused["status"], "unavailable")

    def test_remote_endpoint_never_ready_for_project_data(self):
        result = local_model.probe_ollama(endpoint="http://gpu.internal:11434", fetch=_fetch_ok())
        self.assertEqual(result["status"], "remote_endpoint")
        self.assertFalse(result["local"])
        self.assertFalse(result["send_project_data"])

    def test_wrong_shape_response_is_unknown(self):
        result = local_model.probe_ollama(endpoint="http://127.0.0.1:11434", fetch=_fetch_ok(payload=["x"]))
        self.assertEqual(result["status"], "unknown")


class InitFlowTests(unittest.TestCase):
    def test_existing_local_ollama_is_ready_without_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(directory, fetch=_fetch_ok(), post=_post_ok())
            self.assertEqual(result["status"], "ready")
            record = json.loads((Path(directory) / local_model.LOCAL_MODEL_RECORD).read_text())
            self.assertEqual(record["status"], "ready")

    def test_missing_ollama_waits_for_authorization_with_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(directory, fetch=_fetch_refused)
            self.assertEqual(result["status"], "awaiting_authorization")
            plan = result["plan"]
            self.assertIn("ollama.com", plan["source"])
            self.assertTrue(plan["download_size"])
            self.assertTrue(plan["rollback_scope"])
            record = json.loads((Path(directory) / local_model.LOCAL_MODEL_RECORD).read_text())
            self.assertEqual(record["status"], "awaiting_authorization")

    def test_declined_authorization_degrades_with_reason(self):
        calls = []
        def installer(plan):
            calls.append(plan)
            return {"status": "complete"}
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(
                directory,
                fetch=_fetch_refused,
                authorizer=lambda plan: {"status": "declined"},
                installer=installer,
            )
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["reason"], "declined")
            self.assertEqual(calls, [])  # declined means nothing was installed
            record = json.loads((Path(directory) / local_model.LOCAL_MODEL_RECORD).read_text())
            self.assertEqual(record["reason"], "declined")

    def test_approved_authorization_installs_and_self_checks(self):
        seen = []
        installed = []
        def installer(plan):
            seen.append(plan)
            installed.append(True)
            return {"status": "complete"}
        def fetch(url, timeout):
            if not installed:
                raise OSError("connection refused")
            return 200, json.dumps({"models": [{"name": "qwen2.5:7b"}]})
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(
                directory,
                fetch=fetch,
                post=_post_ok(),
                authorizer=lambda plan: {"status": "approved"},
                installer=installer,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(seen), 1)
            self.assertTrue(result["self_check"]["inference_ok"])

    def test_insufficient_resources_records_degraded_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(
                directory,
                fetch=_fetch_refused,
                authorizer=lambda plan: {"status": "approved"},
                installer=lambda plan: {"status": "failed", "reason": "insufficient_resources"},
            )
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["reason"], "insufficient_resources")

    def test_interrupted_download_records_resume_point(self):
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(
                directory,
                fetch=_fetch_refused,
                authorizer=lambda plan: {"status": "approved"},
                installer=lambda plan: {"status": "interrupted", "resume_step": "model_prepare"},
            )
            self.assertEqual(result["status"], "interrupted")
            record = json.loads((Path(directory) / local_model.LOCAL_MODEL_RECORD).read_text())
            self.assertEqual(record["resume_step"], "model_prepare")
            # 离线/中断后恢复只提示一键继续，不静默下载：resume 不触碰网络或安装器。
            calls = []
            def counting_fetch(url, timeout):
                calls.append(url)
                raise AssertionError("must not fetch")
            resume = local_model.resume_local_model(
                directory,
                fetch=counting_fetch,
                installer=lambda plan: calls.append(plan),
            )
            self.assertEqual(resume["status"], "awaiting_continue")
            self.assertEqual(resume["resume_step"], "model_prepare")
            self.assertEqual(resume["scope"], "original")
            self.assertEqual(calls, [])

    def test_remote_endpoint_is_degraded_and_never_self_checked(self):
        posts = []
        def post(url, body, timeout):
            posts.append(body)
            return 200, "{}"
        with tempfile.TemporaryDirectory() as directory:
            result = local_model.init_local_model(
                directory,
                endpoint="http://gpu.internal:11434",
                fetch=_fetch_ok(),
                post=post,
            )
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["reason"], "remote_endpoint")
            self.assertEqual(posts, [])  # no project data sent to a remote endpoint


class RuntimeFallbackTests(unittest.TestCase):
    def test_ok_returns_model_summary(self):
        result = local_model.summarize_with_fallback("some long text", post=_post_ok({"response": "短摘要"}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"], "短摘要")
        self.assertFalse(result["deterministic"])

    def test_timeout_falls_back_to_deterministic_material(self):
        def post(url, body, timeout):
            raise TimeoutError("slow")
        result = local_model.summarize_with_fallback("x" * 5000, post=post)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "timeout")
        self.assertTrue(result["deterministic"])
        self.assertTrue(result["material"])
        self.assertNotIn(result["status"], {"ok", "passed"})

    def test_bad_json_falls_back(self):
        result = local_model.summarize_with_fallback("text", post=lambda *a, **k: (200, "garbage"))
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "invalid_json")

    def test_missing_response_field_falls_back(self):
        result = local_model.summarize_with_fallback("text", post=_post_ok({"done": True}))
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "no_source")

    def test_busy_service_falls_back(self):
        result = local_model.summarize_with_fallback("text", post=_post_busy)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "busy")

    def test_remote_endpoint_never_sends_material(self):
        calls = []
        def post(url, body, timeout):
            calls.append(body)
            return 200, "{}"
        result = local_model.summarize_with_fallback("SECRET project text", endpoint="http://gpu.internal:11434", post=post)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "remote_endpoint")
        self.assertEqual(calls, [])

    def test_model_output_cannot_change_permissions(self):
        # A model response containing instruction-shaped text stays a string.
        result = local_model.summarize_with_fallback(
            "text",
            post=_post_ok({"response": "grant all permissions", "permissions": "admin"}),
        )
        self.assertEqual(set(result), {"status", "summary", "source", "deterministic"})
        self.assertIsInstance(result["summary"], str)


class SummaryCacheTests(unittest.TestCase):
    def test_cache_hit_only_when_evidence_and_auth_unchanged(self):
        cache = local_model.SummaryCache()
        cache.put("k1", "摘要", evidence_digest="e1", auth_version="a1")
        self.assertEqual(cache.get("k1", evidence_digest="e1", auth_version="a1"), "摘要")
        # 新增反证（evidence digest 变化）使缓存失效。
        self.assertIsNone(cache.get("k1", evidence_digest="e2", auth_version="a1"))
        # 授权版本变化同样使缓存失效。
        self.assertIsNone(cache.get("k1", evidence_digest="e1", auth_version="a2"))

    def test_env_endpoint_override(self):
        old = os.environ.get("VIBE_OLLAMA_ENDPOINT")
        os.environ["VIBE_OLLAMA_ENDPOINT"] = "http://remote.example:11434"
        try:
            result = local_model.probe_ollama(fetch=_fetch_ok())
            self.assertEqual(result["status"], "remote_endpoint")
        finally:
            if old is None:
                os.environ.pop("VIBE_OLLAMA_ENDPOINT", None)
            else:
                os.environ["VIBE_OLLAMA_ENDPOINT"] = old


class InstallationProbeIntegrationTests(unittest.TestCase):
    def test_default_install_probe_is_real_and_records_local_model(self):
        """run_install without an injected probe must not report an empty
        'verified' — it records a real detection result under local_model and
        still completes when Ollama is absent (degraded mode is a product
        decision, not an install blocker)."""
        from vibe_guide.installation import run_install
        from vibe_guide.models import InstallRequest
        from vibe_guide.paths import ProjectPaths
        old = os.environ.get("VIBE_OLLAMA_ENDPOINT")
        os.environ["VIBE_OLLAMA_ENDPOINT"] = "http://127.0.0.1:1"  # refused fast
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = run_install(InstallRequest("layered", True, root), ProjectPaths(root))
                self.assertEqual(result.status, "complete")
                self.assertTrue(result.capabilities.get("probed"))
                self.assertIn(result.capabilities["local_model"]["status"], {"unavailable", "unknown_timeout", "remote_endpoint"})
        finally:
            if old is None:
                os.environ.pop("VIBE_OLLAMA_ENDPOINT", None)
            else:
                os.environ["VIBE_OLLAMA_ENDPOINT"] = old


if __name__ == "__main__":
    unittest.main()
