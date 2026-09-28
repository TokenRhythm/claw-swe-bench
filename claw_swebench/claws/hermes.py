
import logging
import sqlite3
import os
import subprocess
import time
from pathlib import Path

from claw_swebench.config import (
    API_KEY_ENV_VARS,
    CLAW_CONFIGS_DIR,
    CLAW_PYTHON_BIN,
    CLAW_PYTHON_HOME,
    HERMES_ENV_PATH,
    HERMES_SITE_PACKAGES,
)
from claw_swebench.claws.base import BaseClawAdapter, decode_output
from claw_swebench.secrets import render_config_dir
from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_BUFFER = 120


class HermesAdapter(BaseClawAdapter):

    name = "hermes"

    def __init__(self, model: str, timeout: int, max_turns: int | None = None):
        super().__init__(model, timeout, max_turns)
        self.config_dir = render_config_dir("hermes")


    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            "-v", f"{CLAW_PYTHON_HOME}:{CLAW_PYTHON_HOME}:ro",
            "-v", f"{HERMES_ENV_PATH}:{HERMES_ENV_PATH}:ro",
            "-v", f"{self.config_dir}:/opt/hermes-config:ro",
        ]

    def post_container_start(self, workspace) -> None:
        config_path = self.config_dir / "config.yaml"
        if not config_path.exists():
            logger.warning("Hermes config not found at %s", config_path)
        r = workspace.run_in_container(
            "mkdir -p /tmp/hermes-home && "
            "cp /opt/hermes-config/config.yaml /tmp/hermes-home/config.yaml"
        )
        if r.exit_code != 0:
            logger.warning("Failed to provision HERMES_HOME: %s", r.stderr)

    def collect_usage(self, workspace, artifact_dir: Path) -> dict:
        workspace.copy_from_container(
            "/tmp/hermes-home/sessions", str(artifact_dir)
        )
        db_path = artifact_dir / "state.db"
        if not workspace.copy_from_container("/tmp/hermes-home/state.db", str(db_path)):
            return {}
        try:
            con = sqlite3.connect(db_path)
            row = con.execute(
                "select sum(input_tokens), sum(output_tokens), sum(cache_read_tokens), "
                "sum(cache_write_tokens), sum(api_call_count) from sessions"
            ).fetchone()
            con.close()
        except sqlite3.Error as exc:
            logger.warning("Could not read Hermes state.db: %s", exc)
            return {}
        return {
            "input_tokens": row[0] or 0,
            "output_tokens": row[1] or 0,
            "cache_read_tokens": row[2] or 0,
            "cache_write_tokens": row[3] or 0,
            "api_calls": row[4] or 0,
        }


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

        hermes_code = (
            "import sys; "
            f"sys.argv = ['hermes', 'chat', '-q', {repr(prompt)}, "
            f"'--quiet', '--yolo', "
            f"'--max-turns', '{self.max_turns}', "
            f"'--toolsets', 'terminal,file', "
            f"'--model', '{self.model}']; "
            "from hermes_cli.main import main; "
            "sys.exit(main())"
        )

        cmd = [
            "docker", "exec",
            "-e", f"PYTHONPATH={HERMES_SITE_PACKAGES}",
            "-e", "HERMES_HOME=/tmp/hermes-home",
        ]
        for env_name in API_KEY_ENV_VARS:
            val = os.environ.get(env_name)
            if val:
                cmd.extend(["-e", f"{env_name}={val}"])
        cmd.extend([
            container_name,
            CLAW_PYTHON_BIN,
            "-c", hermes_code,
        ])

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
                "Hermes subprocess timed out after %ds",
                self.timeout + SUBPROCESS_TIMEOUT_BUFFER,
            )

        duration = time.time() - start_time

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
