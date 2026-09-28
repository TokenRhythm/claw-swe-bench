
import logging
from pathlib import Path

from claw_swebench.config import PROMPTS_DIR

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_PATH = PROMPTS_DIR / "default.txt"


def build_prompt(
    instance: dict,
    template_path: Path | None = None,
) -> str:
    path = template_path or DEFAULT_TEMPLATE_PATH
    template = path.read_text()

    prompt = template.format(
        problem_statement=instance["problem_statement"],
        base_commit=instance.get("base_commit", ""),
    )
    return prompt


def render_debug_prompt(instance: dict) -> str:
    prompt = build_prompt(instance)
    header = f"=== DEBUG PROMPT for {instance['instance_id']} ===\n"
    footer = "\n=== END ===\n"
    return header + prompt + footer
