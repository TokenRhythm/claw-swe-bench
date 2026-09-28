
import os
from pathlib import Path


DATASET_VERIFIED = "princeton-nlp/SWE-bench_Verified"
DATASET_MULTILINGUAL = "SWE-bench/SWE-bench_Multilingual"
DEFAULT_SPLIT = "test"

DOCKER_IMAGE_PREFIX = "sweb.eval.x86_64"
DOCKER_IMAGE_TAG = "latest"

CONTAINER_PIDS_LIMIT = int(os.environ.get("CLAW_PIDS_LIMIT", "300"))
CONTAINER_MEMORY = os.environ.get("CLAW_CONTAINER_MEMORY", "8g")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
CLAW_CONFIGS_DIR = PROJECT_ROOT / "claw_configs"

SWEBENCH_VENV = Path(os.environ.get("SWEBENCH_VENV", "/data/swe-bench-env"))
SWEBENCH_WORK_DIR = Path(os.environ.get("SWEBENCH_WORK_DIR", "/data"))

CLAW_DEFAULTS = {
    "openclaw": {"model": "openrouter/deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300},
    "hermes":   {"model": "deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300},
    "nanobot":  {"model": "deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300},
    "zeroclaw": {"model": "deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300},
    "generic":  {"model": "deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300, "llm_no": 1},
    "dsh":      {"model": "deepseek/deepseek-v4.1-flash", "timeout": 3600, "max_turns": 300},
}

NETWORK_WHITELIST = os.environ.get("SWE_NETWORK_WHITELIST", "openrouter.ai")

API_KEY_ENV_VARS = (
    "OPENROUTER_API_KEY",
)

DEFAULT_AGENT_TIMEOUT = 3600
DEFAULT_MAX_RETRIES = 1

CLAW_PYTHON_HOME = os.environ.get(
    "CLAW_PYTHON_HOME",
    "/root/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu",
)
CLAW_PYTHON_BIN = f"{CLAW_PYTHON_HOME}/bin/python3.12"

OPENCLAW_NODE_BIN = os.environ.get("OPENCLAW_NODE_BIN", "/usr/bin/node")
OPENCLAW_MODULE_DIR = os.environ.get(
    "OPENCLAW_MODULE_DIR", "/usr/lib/node_modules/openclaw"
)
OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", str(Path.home() / ".openclaw")))

HERMES_ENV_PATH = os.environ.get("HERMES_ENV_PATH", "/opt/hermes-env")
HERMES_SITE_PACKAGES = f"{HERMES_ENV_PATH}/lib/python3.12/site-packages"

NANOBOT_ENV_PATH = os.environ.get("NANOBOT_ENV_PATH", "/opt/nanobot-env")
NANOBOT_SITE_PACKAGES = f"{NANOBOT_ENV_PATH}/lib/python3.12/site-packages"

ZEROCLAW_BIN = os.environ.get("ZEROCLAW_BIN", "/usr/local/bin/zeroclaw")

GA_REPO_PATH = os.environ.get("GA_REPO_PATH", "/opt/genericagent")
GA_ENV_PATH = os.environ.get("GA_ENV_PATH", "/opt/genericagent-env")
GA_SITE_PACKAGES = f"{GA_ENV_PATH}/lib/python3.12/site-packages"
GA_TEMP_ROOT = PROJECT_ROOT / "ga_temp"
GA_MEMORY_ROOT = PROJECT_ROOT / "ga_memory"

DSH_ENV_PATH = os.environ.get("DSH_ENV_PATH", "/opt/dsh-env")
DSH_ENV_PYTHON = f"{DSH_ENV_PATH}/bin/python"
DSH_RUNNER_MOUNT = "/opt/dsh"

DEFAULT_EVAL_TIMEOUT = 1800
DEFAULT_EVAL_WORKERS = 1

SETUP_FILES_TO_REMOVE = (
    "pyproject.toml", "tox.ini", "setup.py",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "Gemfile.lock", "composer.lock",
    "Pipfile.lock", "go.sum",
)

GITIGNORE_PATTERNS = [
    "*.class",
    "*.jar",
    "*.war",
    "*.ear",
    "*.o",
    "*.obj",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.a",
    "*.lib",
    "*.out",
    "*.pyc",
    "*.pyo",
    "__pycache__/",
    "*.egg-info/",
    "/target/",
    "node_modules/",
    "*.exe",
    "*.bin",
]

GIT_USER_EMAIL = "eval@localhost"
GIT_USER_NAME = "eval"


def instance_id_to_image(instance_id: str) -> str:
    return f"{DOCKER_IMAGE_PREFIX}.{instance_id}:{DOCKER_IMAGE_TAG}"


def instance_id_to_image_sweagent(instance_id: str) -> str:
    transformed = instance_id.replace("__", "_1776_").lower()
    return f"swebench/{DOCKER_IMAGE_PREFIX}.{transformed}:{DOCKER_IMAGE_TAG}"


def instance_id_to_container(claw_name: str, instance_id: str) -> str:
    return f"{claw_name}-swe-{instance_id}"


def get_artifact_dir(run_id: str, instance_id: str) -> Path:
    return ARTIFACTS_ROOT / run_id / instance_id


def get_state_path(run_id: str) -> Path:
    return ARTIFACTS_ROOT / run_id / "state.jsonl"


def get_predictions_path(run_id: str) -> Path:
    return ARTIFACTS_ROOT / run_id / "predictions.jsonl"
