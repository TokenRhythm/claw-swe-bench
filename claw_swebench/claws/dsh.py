
import json
import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from claw_swebench.claws.base import BaseClawAdapter, decode_output
from claw_swebench.config import (
    CLAW_PYTHON_HOME,
    DSH_ENV_PATH,
    DSH_ENV_PYTHON,
    DSH_RUNNER_MOUNT,
)
from claw_swebench.secrets import render_config_dir
from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)

RUNNER_SCRIPT = f"{DSH_RUNNER_MOUNT}/dsh_runner.py"
RUNNER_PATCH = f"{DSH_RUNNER_MOUNT}/openrouter-glm.patch.yml"
MODEL_PATCH_OVERRIDES = {
    "deepseek/deepseek-v4.1-flash": f"{DSH_RUNNER_MOUNT}/openrouter-dsv41flash.patch.yml",
}
DSH_PROVIDER = "openrouter"
DEFAULT_REASONING_EFFORT = "xhigh"

SUBPROCESS_TIMEOUT_BUFFER = 120
RESULT_MARKER = "DSH_RESULT_JSON "


class DSHAdapter(BaseClawAdapter):
    name = "dsh"

    def __init__(self, model: str, timeout: int, max_turns: int | None = None,
                 reasoning_effort: str | None = DEFAULT_REASONING_EFFORT):
        super().__init__(model, timeout, max_turns)
        self.runner_dir = render_config_dir("dsh") / "runner"
        self.reasoning_effort = reasoning_effort
        self._registry: dict[str, dict] = {}
        self._reg_lock = threading.Lock()


    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            "-v", f"{CLAW_PYTHON_HOME}:{CLAW_PYTHON_HOME}:ro",
            "-v", f"{DSH_ENV_PATH}:{DSH_ENV_PATH}:ro",
            "-v", f"{self.runner_dir}:{DSH_RUNNER_MOUNT}:ro",
        ]


    def create_agent(self, agent_id: str) -> None:
        with self._reg_lock:
            self._registry[agent_id] = {"container": None, "dsh_home": f"/tmp/dsh_home_{agent_id}"}

    def delete_agent(self, agent_id: str) -> None:
        with self._reg_lock:
            self._registry.pop(agent_id, None)

    def backup_session(self, agent_id: str, dest: Path) -> None:
        with self._reg_lock:
            entry = self._registry.get(agent_id) or {}
        container, dsh_home = entry.get("container"), entry.get("dsh_home")
        if not container or not dsh_home:
            return
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["docker", "cp", f"{container}:{dsh_home}/sessions", str(dest / "dsh_sessions")],
            capture_output=True, text=True,
        )


    def send_task(
        self,
        prompt: str,
        agent_id: str,
        container_name: str,
        artifact_dir: Path | None = None,
        instance_id: str | None = None,
    ) -> AgentResult:
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "agent_stdout.log" if artifact_dir else None
        stderr_path = artifact_dir / "agent_stderr.log" if artifact_dir else None

        with self._reg_lock:
            entry = self._registry.setdefault(
                agent_id, {"container": None, "dsh_home": f"/tmp/dsh_home_{agent_id}"})
            entry["container"] = container_name
            dsh_home = entry["dsh_home"]

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set in host environment")

        task_file = f"/tmp/dsh_task_{agent_id}.txt"
        cp = subprocess.run(
            ["docker", "exec", "-i", container_name, "bash", "-c", f"cat > {shlex.quote(task_file)}"],
            input=prompt, text=True, capture_output=True, timeout=60,
        )
        if cp.returncode != 0:
            raise RuntimeError(f"Failed to stage prompt into container {container_name}: {cp.stderr}")

        cmd = [
            "docker", "exec", "-e", f"OPENROUTER_API_KEY={api_key}", container_name,
            DSH_ENV_PYTHON, RUNNER_SCRIPT,
            "--prompt-file", task_file,
            "--model", self.model,
            "--provider", DSH_PROVIDER,
            "--session-id", agent_id,
            "--dsh-home", dsh_home,
            "--patch", MODEL_PATCH_OVERRIDES.get(self.model, RUNNER_PATCH),
            "--cwd", "/testbed",
            *(["--reasoning-effort", self.reasoning_effort] if self.reasoning_effort else []),
        ]

        start_time = time.time()
        timed_out = False
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self.timeout + SUBPROCESS_TIMEOUT_BUFFER)
            exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout, stderr = decode_output(e.stdout), decode_output(e.stderr)
            logger.warning("DSH subprocess timed out after %ds", self.timeout + SUBPROCESS_TIMEOUT_BUFFER)
        duration = time.time() - start_time

        if stdout_path:
            stdout_path.write_text(stdout)
        if stderr_path:
            stderr_path.write_text(stderr)

        parsed = self._parse_result(stdout) or self._parse_result(stderr)
        if timed_out:
            finish_reason = "timeout"
        elif parsed is None or parsed.get("status") != "ok":
            finish_reason = "error"
        else:
            final = (parsed.get("final_response") or "").strip()
            out_tokens = (parsed.get("usage") or {}).get("outputTokens", 0)
            finish_reason = "empty" if (not final and not out_tokens) else "stop"

        return AgentResult(
            success=finish_reason == "stop",
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish_reason,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            session_id=(parsed.get("session_id") if parsed else agent_id),
            duration_seconds=round(duration, 1),
            usage=(parsed.get("usage", {}) if parsed else {}),
        )

    @staticmethod
    def _parse_result(text: str) -> dict | None:
        if not text:
            return None
        idx = text.rfind(RESULT_MARKER)
        if idx == -1:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[idx + len(RESULT_MARKER):].lstrip())
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
