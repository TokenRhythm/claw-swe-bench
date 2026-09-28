"""Offline regression tests for Meta-Harness process and artifact contracts."""

import dataclasses
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "metaharness"))

import hermes_wrapper as hw
import meta_harness as mh
from claw_swebench.claws import generic


def completed_metadata():
    return {
        "state": "patch_collected",
        "patch_empty": False,
        "agent": {"success": True, "finish_reason": "stop", "exit_code": 0},
    }


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="metaharness-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.object(mh, "EVOLVE_DIR", self.root).start()
        patch.object(mh, "JOBS_DIR", self.root / "jobs" / "experiment-a").start()
        patch.object(mh, "ARTIFACTS_DIR", self.root / "artifacts").start()
        self.output = io.StringIO()
        output_capture = redirect_stdout(self.output)
        output_capture.__enter__()
        self.addCleanup(output_capture.__exit__, None, None, None)

    def smoke(self, metadata, returncode=0):
        def infer(cmd, **kwargs):
            rid = cmd[cmd.index("--run_id") + 1]
            iid = cmd[cmd.index("--instance_ids") + 1]
            if metadata is not None:
                target = mh.ARTIFACTS_DIR / rid / iid / "metadata.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(metadata))
            return subprocess.CompletedProcess(cmd, returncode, "", "")

        with patch.object(mh, "run_cmd", infer):
            return mh.smoke_test("candidate", "candidate")

    def test_smoke_accepts_normal_semantic_completion(self):
        self.assertTrue(self.smoke(completed_metadata()))

    def test_smoke_accepts_real_adapter_stop_with_terminated_exit(self):
        adapter = generic.GenericAgentAdapter.__new__(generic.GenericAgentAdapter)
        adapter.timeout, adapter.llm_no = 10, 1
        temp_root = self.root / "ga-temp"
        sentinel = temp_root / "iid" / "agent" / "output.txt"
        real_popen = subprocess.Popen

        def launch(cmd, **kwargs):
            self.assertEqual(cmd[:2], ["docker", "exec"])
            script = (
                "from pathlib import Path; import time; "
                f"Path({str(sentinel)!r}).write_text('[ROUND END]'); time.sleep(30)"
            )
            return real_popen([sys.executable, "-c", script], **kwargs)

        with patch.object(generic, "GA_TEMP_ROOT", temp_root), patch.object(generic.subprocess, "Popen", launch):
            result = adapter.send_task("test", "agent", "unused", instance_id="iid")
        self.assertTrue(result.success)
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.exit_code, -15)
        metadata = completed_metadata()
        metadata["agent"] = dataclasses.asdict(result)
        self.assertTrue(self.smoke(metadata))
        job = json.loads((mh.JOBS_DIR / "smoke-candidate" / "job.json").read_text())
        saved = mh.ARTIFACTS_DIR / job["run_ids"][0] / mh.SMOKE_TEST_TASK[1] / "metadata.json"
        self.assertEqual(json.loads(saved.read_text())["agent"]["exit_code"], -15)

    def test_smoke_rejects_failed_timeout_and_incomplete_results(self):
        cases = []
        for state in ("failed", "timeout", "pending", "running", "skipped", None):
            metadata = completed_metadata()
            metadata["state"] = state
            cases.append((f"state={state}", metadata))
        for field, value in (("success", False), ("success", None), ("finish_reason", "timeout"),
                             ("finish_reason", "error"), ("finish_reason", None), ("timeout", True)):
            metadata = completed_metadata()
            metadata["agent"][field] = value
            cases.append((f"agent {field}={value}", metadata))
        for field in ("agent", "state", "patch_empty"):
            metadata = completed_metadata()
            del metadata[field]
            cases.append((f"missing {field}", metadata))
        for value in (True, None):
            metadata = completed_metadata()
            metadata["patch_empty"] = value
            cases.append((f"patch_empty={value}", metadata))
        metadata = completed_metadata()
        metadata["agent"] = {"exit_code": 9, "success": False, "finish_reason": "error"}
        cases.append(("nonzero error", metadata))
        for label, metadata in cases:
            with self.subTest(label=label):
                self.assertFalse(self.smoke(metadata))

    def test_smoke_rejects_missing_metadata(self):
        self.assertFalse(self.smoke(None))

    def test_smoke_rejects_inference_failure_and_timeout(self):
        for code in (1, 124):
            with self.subTest(code=code):
                self.assertFalse(self.smoke(completed_metadata(), returncode=code))

    def benchmark(self, namespace, date):
        commands, evaluations = [], []

        def infer(cmd, **kwargs):
            commands.append(cmd)
            rid = cmd[cmd.index("--run_id") + 1]
            predictions = mh.ARTIFACTS_DIR / rid / "predictions.jsonl"
            predictions.parent.mkdir(parents=True, exist_ok=True)
            predictions.write_text('{}\n')
            return subprocess.CompletedProcess(cmd, 0, "", "")

        class Evaluation:
            returncode = 0

            def wait(self):
                return self.returncode

        def evaluate(cmd, **kwargs):
            evaluations.append(cmd)
            return Evaluation()

        with patch.object(mh, "JOBS_DIR", self.root / "jobs" / namespace), \
                patch.object(mh, "datetime") as clock, patch.object(mh, "run_cmd", infer), \
                patch.object(mh.subprocess, "Popen", evaluate):
            clock.now.return_value = date
            job, ok = mh.swebench_run("candidate", "evolve-candidate-t1")
        self.assertTrue(ok)
        ids = json.loads((job / "job.json").read_text())["run_ids"]
        self.assertEqual(ids, [cmd[cmd.index("--run_id") + 1] for cmd in commands])
        self.assertEqual(ids, [cmd[cmd.index("--run_id") + 1] for cmd in evaluations])
        self.assertEqual([cmd[cmd.index("--dataset") + 1] for cmd in commands], ["verified", "multilingual"])
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all("--no_resume" not in cmd for cmd in commands))
        return ids

    def test_benchmark_namespaces_separate_both_datasets(self):
        a = self.benchmark("experiment-a", datetime(2026, 9, 28))
        b = self.benchmark("experiment-b", datetime(2026, 9, 28))
        self.assertTrue(set(a).isdisjoint(b))

    def test_benchmark_nested_namespaces_with_same_leaf_are_distinct(self):
        a = self.benchmark("group-a/trial", datetime(2026, 9, 28))
        b = self.benchmark("group-b/trial", datetime(2026, 9, 28))
        self.assertTrue(set(a).isdisjoint(b))
        self.assertEqual(a, self.benchmark("group-a/trial", datetime(2026, 9, 29)))

    def test_benchmark_same_named_run_resumes_across_dates(self):
        a = self.benchmark("experiment-a", datetime(2026, 9, 28))
        self.assertEqual(a, self.benchmark("experiment-a", datetime(2026, 9, 28)))
        self.assertEqual(a, self.benchmark("experiment-a", datetime(2026, 9, 29)))

    def test_repeated_smoke_uses_fresh_artifacts_and_namespaces(self):
        ids = []

        def infer(cmd, **kwargs):
            rid = cmd[cmd.index("--run_id") + 1]
            ids.append(rid)
            self.assertIn("--no_resume", cmd)
            directory = mh.ARTIFACTS_DIR / rid
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "predictions.jsonl").open("a") as predictions:
                predictions.write('{}\n')
            iid = cmd[cmd.index("--instance_ids") + 1]
            (directory / iid).mkdir(exist_ok=True)
            (directory / iid / "metadata.json").write_text(json.dumps(completed_metadata()))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(mh, "run_cmd", infer), patch.object(mh, "datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 28)
            self.assertTrue(mh.smoke_test("candidate", "candidate"))
            self.assertTrue(mh.smoke_test("candidate", "candidate"))
            with patch.object(mh, "JOBS_DIR", self.root / "jobs" / "experiment-b"):
                self.assertTrue(mh.smoke_test("candidate", "candidate"))
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("experiment-a", ids[0])
        self.assertIn("experiment-b", ids[2])
        for rid in ids:
            self.assertEqual((mh.ARTIFACTS_DIR / rid / "predictions.jsonl").read_text(), '{}\n')


class ProposerTimeoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="proposer-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def invoke(self, launch, timeout=1):
        with patch.object(hw, "render_config_dir", return_value=self.root / "config"), \
                patch.object(hw, "_api_key", return_value="dummy"), \
                patch.object(hw.subprocess, "run", launch):
            return hw.run("test", log_dir=self.root / "logs", timeout_seconds=timeout)

    def assert_timeout(self, result, stdout, stderr, timeout=1):
        self.assertEqual(result.exit_code, 124)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, stderr + f"\n[timeout after {timeout}s]")
        saved = json.loads(Path(result.log_path).read_text())
        self.assertEqual(saved["exit_code"], 124)
        self.assertEqual(saved["stdout"], result.stdout)
        self.assertEqual(saved["stderr"], result.stderr)
        self.assertEqual(saved["session_id"], result.session_id)
        self.assertEqual(saved["usage"], result.usage)

    def test_real_subprocess_timeout_preserves_captured_bytes(self):
        real_run = subprocess.run

        def launch(cmd, **kwargs):
            self.assertEqual(cmd[0], "hermes")
            script = ('import sys,time; print("out",flush=True); '
                      'print("err",file=sys.stderr,flush=True); time.sleep(30)')
            return real_run([sys.executable, "-c", script], capture_output=True, text=True, timeout=0.2)

        result = self.invoke(launch, timeout=0.2)
        self.assert_timeout(result, "out\n", "err\n", timeout=0.2)

    def test_timeout_handles_bytes_text_and_missing_streams(self):
        cases = [
            (b"out\xff", b"session_id: abc\nusage: {\"input_tokens\": 3}\nerr\xff", "out\ufffd",
             'session_id: abc\nusage: {"input_tokens": 3}\nerr\ufffd'),
            ("out", 'session_id: abc\nusage: {"input_tokens": 3}', "out",
             'session_id: abc\nusage: {"input_tokens": 3}'),
            (b"stdout only", None, "stdout only", ""),
            (None, b"stderr only", "", "stderr only"),
            (None, None, "", ""),
        ]
        for stdout, stderr, expected_out, expected_err in cases:
            with self.subTest(stdout=stdout, stderr=stderr):
                error = subprocess.TimeoutExpired("hermes", 1, output=stdout, stderr=stderr)

                def launch(cmd, **kwargs):
                    raise error

                result = self.invoke(launch)
                self.assert_timeout(result, expected_out, expected_err)
                if "session_id" in expected_err:
                    self.assertEqual(result.session_id, "abc")
                    self.assertEqual(result.usage, {"input_tokens": 3})


if __name__ == "__main__":
    unittest.main()
