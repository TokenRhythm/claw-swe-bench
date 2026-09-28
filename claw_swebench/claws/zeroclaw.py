
import json
import logging
import os
import sys
import socket
import subprocess
import time
from pathlib import Path

from claw_swebench.config import CLAW_CONFIGS_DIR, PROJECT_ROOT, ZEROCLAW_BIN
from claw_swebench.claws.base import BaseClawAdapter, decode_output
from claw_swebench.secrets import render_config_dir, require_openrouter_base_url
from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_BUFFER = 120


PROXY_USAGE_LOG = Path(os.environ.get("PROXY_USAGE_LOG", "/tmp/zc_proxy_usage.jsonl"))
PROXY_PORT = int(os.environ.get("ZEROCLAW_PROXY_PORT", "18090"))


class ZeroClawAdapter(BaseClawAdapter):

    name = "zeroclaw"

    def __init__(self, model: str, timeout: int, max_turns: int | None = None):
        super().__init__(model, timeout, max_turns)
        self.config_dir = render_config_dir("zeroclaw")
        self._ensure_proxy()

    @staticmethod
    def _ensure_proxy():
        upstream = require_openrouter_base_url()
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", PROXY_PORT)) == 0:
                return
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        log = open(log_dir / "zeroclaw_proxy.log", "a")
        subprocess.Popen(
            [sys.executable, str(CLAW_CONFIGS_DIR / "zeroclaw" / "tool_filter_proxy.py"),
             upstream, str(PROXY_PORT), str(PROXY_USAGE_LOG)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        for _ in range(20):
            time.sleep(0.5)
            with socket.socket() as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", PROXY_PORT)) == 0:
                    logger.info("Started ZeroClaw tool-filter proxy on :%d", PROXY_PORT)
                    return
        raise RuntimeError(f"tool_filter_proxy did not come up on :{PROXY_PORT}; see logs/zeroclaw_proxy.log")


    def container_run_args(self, instance_id: str) -> list[str]:
        return [
            "-v", f"{ZEROCLAW_BIN}:/usr/local/bin/zeroclaw:ro",
        ]

    def post_container_start(self, workspace) -> None:
        config_src = self.config_dir / "config.toml"
        if not config_src.exists():
            logger.warning("ZeroClaw config not found at %s", config_src)
            return
        workspace.run_in_container("mkdir -p /tmp/zeroclaw-workspace")
        workspace.copy_to_container(
            str(config_src), "/tmp/zeroclaw-workspace/config.toml"
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

        cmd = [
            "docker", "exec",
            "-e", "ZEROCLAW_WORKSPACE=/tmp/zeroclaw-workspace",
            container_name,
            "zeroclaw", "agent", "-m", prompt,
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
                "ZeroClaw subprocess timed out after %ds",
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


    def collect_usage(self, workspace, artifact_dir: Path) -> dict:
        workspace.copy_from_container(
            "/tmp/zeroclaw-workspace/workspace/state/costs.jsonl",
            str(artifact_dir / "costs.jsonl"),
        )
        container_ip = workspace.get_container_ip()
        return _parse_costs(artifact_dir, client_ip=container_ip, model=self.model)


def _parse_proxy_usage(start_ts: str, end_ts: str, client_ip: str = "") -> dict:
    if not PROXY_USAGE_LOG.exists():
        return {}
    total_cache_read = 0
    total_reasoning = 0
    try:
        with open(PROXY_USAGE_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if ts < start_ts or ts > end_ts:
                    continue
                if client_ip and entry.get("client_ip", "") != client_ip:
                    continue
                usage = entry.get("usage", {})
                cache_hit = usage.get("prompt_cache_hit_tokens", 0)
                if not cache_hit:
                    cached_detail = usage.get("prompt_tokens_details", {})
                    cache_hit = cached_detail.get("cached_tokens", 0) if cached_detail else 0
                total_cache_read += cache_hit
                comp_detail = usage.get("completion_tokens_details", {})
                if comp_detail:
                    total_reasoning += comp_detail.get("reasoning_tokens", 0)
    except (json.JSONDecodeError, KeyError):
        pass
    result = {}
    if total_cache_read > 0:
        result["cache_read_tokens"] = total_cache_read
    if total_reasoning > 0:
        result["reasoning_tokens"] = total_reasoning
    return result


def _parse_costs(artifact_dir: Path, client_ip: str = "", model: str = "") -> dict:
    costs_path = artifact_dir / "costs.jsonl"
    if not costs_path.exists():
        return {}
    turns = 0
    total_input = 0
    total_output = 0
    total_tokens = 0
    first_ts = ""
    last_ts = ""
    try:
        with open(costs_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                turns += 1
                usage = entry.get("usage", {})
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
                ts = usage.get("timestamp", "")
                if ts and not first_ts:
                    first_ts = ts
                if ts:
                    last_ts = ts
    except (json.JSONDecodeError, KeyError):
        pass
    result = {
        "turns": turns,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_tokens,
    }
    if first_ts and last_ts:
        proxy_data = _parse_proxy_usage(first_ts, last_ts, client_ip)
        result.update(proxy_data)
    return result
