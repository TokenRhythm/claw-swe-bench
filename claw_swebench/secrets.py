
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from claw_swebench.config import CLAW_CONFIGS_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

RENDERED_ROOT = PROJECT_ROOT / ".rendered"
PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".py", ".txt", ".cfg", ".ini"}


def require_openrouter_base_url() -> str:
    value = os.environ.get("OPENROUTER_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("OPENROUTER_BASE_URL is not set; export your API base URL before running.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(c.isspace() for c in value):
            raise ValueError
    except ValueError:
        raise RuntimeError("OPENROUTER_BASE_URL must be an absolute HTTP(S) API base URL.") from None
    return value.rstrip("/")


def render_config_dir(claw: str) -> Path:
    src = CLAW_CONFIGS_DIR / claw
    dst = RENDERED_ROOT / claw
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    missing: set[str] = set()
    for f in dst.rglob("*"):
        if not f.is_file() or f.suffix not in TEXT_SUFFIXES:
            continue
        text = f.read_text(errors="replace")
        if "${" not in text:
            continue

        def sub(m):
            if m.group(1) == "OPENROUTER_BASE_URL":
                return require_openrouter_base_url()
            val = os.environ.get(m.group(1))
            if val is None:
                missing.add(m.group(1))
                return ""
            return val

        f.write_text(PLACEHOLDER.sub(sub, text))
    if missing:
        logger.warning("%s config: environment variables not set, left empty: %s",
                       claw, ", ".join(sorted(missing)))
    return dst
