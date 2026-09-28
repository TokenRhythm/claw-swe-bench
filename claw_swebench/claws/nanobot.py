
import json
import logging
import subprocess
import re
import time
from pathlib import Path

from claw_swebench.config import (
    CLAW_CONFIGS_DIR,
    CLAW_PYTHON_BIN,
    CLAW_PYTHON_HOME,
    NANOBOT_ENV_PATH,
    NANOBOT_SITE_PACKAGES,
)
from claw_swebench.claws.base import BaseClawAdapter, decode_output
from claw_swebench.secrets import render_config_dir
from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)

USAGE_RE = re.compile(r"LLM usage: prompt=(\d+) completion=(\d+) cached=(\d+)")

SUBPROCESS_TIMEOUT_BUFFER = 120


class NanoBotAdapter(BaseClawAdapter):

    name = "nanobot"

    CONTEXT_WINDOW_TOKENS = 200_000

    def __init__(self, model: str, timeout: int, max_turns: int | None = None):
        super().__init__(model, timeout, max_turns)
        self.config_dir = render_config_dir("nanobot")
        cfg_path = self.config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
        defaults["context_window_tokens"] = self.CONTEXT_WINDOW_TOKENS
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        logger.info("NanoBot config: model=%s context_window_tokens=%d",
                    defaults.get("model"), self.CONTEXT_WINDOW_TOKENS)


    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            "-v", f"{CLAW_PYTHON_HOME}:{CLAW_PYTHON_HOME}:ro",
            "-v", f"{NANOBOT_ENV_PATH}:{NANOBOT_ENV_PATH}:ro",
            "-v", f"{self.config_dir}:/opt/nanobot-config",
        ]


    def collect_usage(self, workspace, artifact_dir: Path) -> dict:
        log = artifact_dir / "agent_stderr.log"
        if not log.exists():
            return {}
        prompt = completion = cached = calls = 0
        for m in USAGE_RE.finditer(log.read_text(errors="replace")):
            p, c, k = map(int, m.groups())
            prompt += p; completion += c; cached += k; calls += 1
        if not calls:
            return {}
        return {"input_tokens": prompt - cached, "cache_read_tokens": cached,
                "output_tokens": completion, "api_calls": calls}

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

        nanobot_code = (
            "import sys; "
            f"sys.argv = ['nanobot', 'agent', '-m', {repr(prompt)}, "
            f"'-c', '/opt/nanobot-config/config.json', "
            f"'-w', '/testbed', "
            f"'--no-markdown', '--logs']; "
            "from nanobot.cli.commands import app; "
            "app()"
        )

        cmd = [
            "docker", "exec",
            "-e", f"PYTHONPATH={NANOBOT_SITE_PACKAGES}",
            container_name,
            CLAW_PYTHON_BIN,
            "-c", nanobot_code,
        ]

        start_time = time.time()
        timed_out = False

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
            )
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = -1
            stdout = decode_output(e.stdout)
            stderr = decode_output(e.stderr)
            logger.warning(
                "NanoBot subprocess timed out after %ds",
                self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
            )

        duration = time.time() - start_time

        if artifact_dir:
            _save_session_jsonl(container_name, artifact_dir)

        _cleanup_nanobot_metadata(container_name)

        if stdout_path:
            stdout_path.write_text(stdout)
        if stderr_path:
            stderr_path.write_text(stderr)

        if timed_out:
            finish_reason = "timeout"
        elif exit_code != 0:
            finish_reason = "error"
        elif not stdout or not stdout.strip():
            finish_reason = "empty"
        else:
            finish_reason = "stop"

        return AgentResult(
            success=finish_reason == "stop",
            timeout=timed_out,
            exit_code=exit_code,
            finish_reason=finish_reason,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            session_id=None,
            duration_seconds=round(duration, 1),
            usage={},
        )


def _save_session_jsonl(container_name: str, artifact_dir: Path) -> None:
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "cat",
             "/testbed/sessions/cli_direct.jsonl"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            (artifact_dir / "session.jsonl").write_text(result.stdout)
    except Exception:
        pass


def _cleanup_nanobot_metadata(container_name: str) -> None:
    subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         "cd /testbed && rm -rf "
         "AGENTS.md HEARTBEAT.md SOUL.md TOOLS.md USER.md "
         "memory/ sessions/ .nanobot/"],
        capture_output=True, timeout=10,
    )
