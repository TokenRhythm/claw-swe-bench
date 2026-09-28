
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class InstanceState(str, Enum):

    PENDING = "pending"
    RUNNING = "running"
    PATCH_COLLECTED = "patch_collected"
    EVAL_DONE = "eval_done"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class AgentResult:

    success: bool
    timeout: bool
    exit_code: int
    finish_reason: str
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    session_id: str | None = None
    duration_seconds: float = 0.0
    usage: dict = field(default_factory=dict)


@dataclass
class InstanceRecord:

    instance_id: str
    state: InstanceState
    model: str
    run_id: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    patch_empty: bool | None = None
    error: str | None = None
