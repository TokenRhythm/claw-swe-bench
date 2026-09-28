
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from claw_swebench.secrets import render_config_dir

DEFAULT_LOG_DIR = HERE / "logs" / "proposer_sessions"

PROPOSER_MODEL = "deepseek/deepseek-v4.1-flash"
PROPOSER_TOOLSETS = "terminal,file"


@dataclass
class SessionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    session_id: str | None = None
    usage: dict = field(default_factory=dict)
    log_path: str | None = None

    def show(self):
        u = self.usage
        print(
            f"  proposer done exit={self.exit_code} {self.duration:.0f}s "
            f"session={self.session_id} in={u.get('input_tokens')} "
            f"cache={u.get('cache_read_tokens')} out={u.get('output_tokens')} "
            f"${u.get('estimated_cost_usd', 0):.3f}"
        )


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY must be set for the proposer")
    return key


def run(
    prompt,
    cwd=None,
    log_dir=None,
    name=None,
    timeout_seconds=2400,
    skills=None,
    max_turns=300,
    model=PROPOSER_MODEL,
    **_ignored,
):
    log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    cwd = str(cwd or HERE)

    prior = ""
    for s in skills or []:
        p = Path(s)
        p = p / "SKILL.md" if p.is_dir() else p
        if p.exists():
            prior += p.read_text() + "\n\n"
    full_prompt = (prior + prompt) if prior else prompt

    env = os.environ.copy()
    env["HERMES_HOME"] = str(render_config_dir("proposer"))
    env["OPENROUTER_API_KEY"] = _api_key()
    env.pop("CLAUDECODE", None)

    cmd = [
        "hermes", "chat", "-q", full_prompt,
        "--quiet", "--yolo",
        "--toolsets", PROPOSER_TOOLSETS,
        "--max-turns", str(max_turns),
        "--model", model,
        "--ignore-rules",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_seconds)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = 124, (e.stdout or ""), (e.stderr or "") + f"\n[timeout after {timeout_seconds}s]"
    dur = time.time() - t0

    sid = None
    usage = {}
    m = re.search(r"session_id:\s*(\S+)", err)
    if m:
        sid = m.group(1)
    m = re.search(r"usage:\s*(\{.*\})", err)
    if m:
        try:
            usage = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{name or 'proposer'}_{stamp}.json"
    log_path.write_text(json.dumps({
        "name": name, "model": model, "cwd": cwd, "exit_code": code, "duration_s": round(dur, 1),
        "session_id": sid, "usage": usage, "prompt": full_prompt, "stdout": out, "stderr": err,
    }, indent=2, ensure_ascii=False))
    return SessionResult(code, out, err, dur, sid, usage, str(log_path))
