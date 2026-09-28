
import logging
from pathlib import Path

from claw_swebench.types import AgentResult

logger = logging.getLogger(__name__)


class BaseClawAdapter:

    name = "base"

    def __init__(
        self,
        model: str,
        timeout: int,
        max_turns: int | None = None,
    ):
        self.model = model
        self.timeout = timeout
        self.max_turns = max_turns


    def container_run_args(self, instance_id: str) -> list[str]:
        return []

    def post_container_start(self, workspace) -> None:
        pass


    def create_agent(self, agent_id: str) -> None:
        pass

    def delete_agent(self, agent_id: str) -> None:
        pass

    def backup_session(self, agent_id: str, dest: Path) -> None:
        pass

    def switch_model(self, model_name: str) -> None:
        self.model = model_name
        logger.info("Model set to %s", model_name)


    def send_task(
        self,
        prompt: str,
        agent_id: str,
        container_name: str,
        artifact_dir: Path | None = None,
        instance_id: str | None = None,
    ) -> AgentResult:
        raise NotImplementedError

    def collect_usage(self, workspace, artifact_dir: Path) -> dict:
        return {}

    def prompt_template(self) -> Path | None:
        return None


def decode_output(data) -> str:
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""
