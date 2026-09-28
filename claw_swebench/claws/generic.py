
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from claw_swebench.config import (
    API_KEY_ENV_VARS,
    CLAW_CONFIGS_DIR,
    CLAW_PYTHON_BIN,
    CLAW_PYTHON_HOME,
    GA_ENV_PATH,
    GA_MEMORY_ROOT,
    GA_REPO_PATH,
    GA_SITE_PACKAGES,
    GA_TEMP_ROOT,
    PROMPTS_DIR,
)
from claw_swebench.claws.base import BaseClawAdapter, decode_output
from claw_swebench.secrets import require_openrouter_base_url
from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)

ROUND_END = "[ROUND END]"

GA_CONFIG_DIR = CLAW_CONFIGS_DIR / "generic"

FORWARDED_ENV_VARS = API_KEY_ENV_VARS + ("OPENROUTER_BASE_URL", "GA_LANG")

ANTHROPIC_USAGE_RE = re.compile(
    r'\[Cache\]\s*input=(\d+)\s*creation=(\d+)\s*read=(\d+)'
)
OAI_INPUT_RE = re.compile(r'\[Cache\]\s*input=(\d+)\s*cached=(\d+)')
OAI_OUTPUT_RE = re.compile(r'\[Output\]\s*tokens=(\d+)')


class GenericAgentAdapter(BaseClawAdapter):

    name = "generic"

    def __init__(
        self,
        model: str,
        timeout: int,
        max_turns: int | None = None,
        llm_no: int = 0,
        candidate_dir: str | Path | None = None,
    ):
        super().__init__(model, timeout, max_turns)
        require_openrouter_base_url()
        self.llm_no = llm_no
        self.source_dir = Path(candidate_dir).resolve() if candidate_dir else Path(GA_REPO_PATH)
        if not (self.source_dir / "agentmain.py").exists():
            raise FileNotFoundError(f"GenericAgent tree missing agentmain.py: {self.source_dir}")
        if candidate_dir:
            self.name = f"generic-{self.source_dir.name}"
        (self.source_dir / "temp").mkdir(exist_ok=True)
        (self.source_dir / "memory").mkdir(exist_ok=True)


    def container_run_args(self, instance_id: str) -> list[str]:
        host_ga_temp = GA_TEMP_ROOT / instance_id
        host_ga_temp.mkdir(parents=True, exist_ok=True)

        host_ga_memory = GA_MEMORY_ROOT / instance_id
        if host_ga_memory.exists():
            shutil.rmtree(host_ga_memory)
        shutil.copytree(self.source_dir / "memory", host_ga_memory)

        return [
            "-v", f"{CLAW_PYTHON_HOME}:{CLAW_PYTHON_HOME}:ro",
            "-v", f"{self.source_dir}:{GA_REPO_PATH}:ro",
            "-v", f"{GA_ENV_PATH}:{GA_ENV_PATH}:ro",
            "-v", f"{GA_CONFIG_DIR}:/opt/generic-config:ro",
            "-v", f"{host_ga_temp}:{GA_REPO_PATH}/temp:rw",
            "-v", f"{host_ga_memory}:{GA_REPO_PATH}/memory:rw",
        ]


    def backup_session(self, agent_id: str, dest: Path) -> None:
        if not dest:
            return
        host_temp = None
        if GA_TEMP_ROOT.exists():
            for d in GA_TEMP_ROOT.iterdir():
                cand = d / agent_id
                if cand.exists():
                    host_temp = cand
                    break
        if host_temp is None:
            return
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("output.txt", "stdout.log", "stderr.log", "input.txt"):
            src = host_temp / name
            if src.exists():
                (dest / name).write_bytes(src.read_bytes())
        model_resp_dir = host_temp / "model_responses"
        if model_resp_dir.exists():
            (dest / "model_responses").mkdir(exist_ok=True)
            for f in model_resp_dir.glob("model_responses_*.txt"):
                (dest / "model_responses" / f.name).write_bytes(f.read_bytes())


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

        if instance_id is None:
            instance_id = agent_id
        host_temp = GA_TEMP_ROOT / instance_id / agent_id
        host_temp.mkdir(parents=True, exist_ok=True)
        for stale in host_temp.glob("output*.txt"):
            stale.unlink()
        for stale in (host_temp / "reply.txt",):
            if stale.exists():
                stale.unlink()

        (host_temp / "input.txt").write_text(prompt, encoding="utf-8")

        stdout_path = artifact_dir / "agent_stdout.log" if artifact_dir else None
        stderr_path = artifact_dir / "agent_stderr.log" if artifact_dir else None

        cmd = [
            "docker", "exec",
            "-w", "/testbed",
            "-e", f"PYTHONPATH=/opt/generic-config:{GA_SITE_PACKAGES}:{GA_REPO_PATH}",
            "-e", "GA_LANG=en",
        ]
        for env_name in FORWARDED_ENV_VARS:
            val = os.environ.get(env_name)
            if val:
                cmd.extend(["-e", f"{env_name}={val}"])
        cmd.extend([
            container_name,
            CLAW_PYTHON_BIN,
            f"{GA_REPO_PATH}/agentmain.py",
            "--task", agent_id,
            "--llm_no", str(self.llm_no),
            "--nobg",
            "--verbose",
        ])

        start_time = time.time()
        timed_out = False
        exit_code = 0
        stdout_text = ""
        stderr_text = ""

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        def _drain(stream, sink):
            try:
                for chunk in iter(lambda: stream.read(65536), b""):
                    sink.append(chunk)
            finally:
                stream.close()

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True),
        ]
        for t in readers:
            t.start()

        output_path = host_temp / "output.txt"
        sentinel_seen = False
        deadline = start_time + self.timeout

        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if output_path.exists():
                    try:
                        txt = output_path.read_text(encoding="utf-8", errors="replace")
                        if ROUND_END in txt:
                            sentinel_seen = True
                            break
                    except OSError:
                        pass
                time.sleep(2)
            else:
                timed_out = True
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            for t in readers:
                t.join(timeout=10)
            stdout_text = decode_output(b"".join(out_chunks))
            stderr_text = decode_output(b"".join(err_chunks))
            exit_code = proc.returncode if proc.returncode is not None else -1

        duration = time.time() - start_time

        if stdout_path:
            stdout_path.write_text(stdout_text or "")
        if stderr_path:
            stderr_path.write_text(stderr_text or "")

        usage = _parse_usage_text(stdout_text or "")

        if timed_out and not sentinel_seen:
            finish_reason = "timeout"
        elif not sentinel_seen:
            finish_reason = "error"
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
            usage=usage,
        )


def _parse_usage_text(text: str) -> dict:
    total = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    if not text:
        return total

    anthropic_spans = []
    for m in ANTHROPIC_USAGE_RE.finditer(text):
        total["input"] += int(m.group(1))
        total["cacheWrite"] += int(m.group(2))
        total["cacheRead"] += int(m.group(3))
        anthropic_spans.append((m.start(), m.end()))

    for m in OAI_INPUT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in anthropic_spans):
            continue
        total["input"] += int(m.group(1))
        total["cacheRead"] += int(m.group(2))

    for m in OAI_OUTPUT_RE.finditer(text):
        total["output"] += int(m.group(1))

    return total
