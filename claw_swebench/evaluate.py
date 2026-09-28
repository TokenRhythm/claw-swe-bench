
import logging
import subprocess
from pathlib import Path

from claw_swebench.config import SWEBENCH_VENV, SWEBENCH_WORK_DIR
from claw_swebench.prediction import validate_prediction_file

logger = logging.getLogger(__name__)


def run_evaluation(
    predictions_path: str | Path,
    dataset_name: str,
    run_id: str,
    instance_ids: list[str] | None = None,
    max_workers: int = 1,
    timeout: int = 1800,
) -> int:
    predictions_path = Path(predictions_path)

    errors = validate_prediction_file(predictions_path)
    if errors:
        for e in errors:
            logger.error("Prediction validation: %s", e)
        raise ValueError(f"Prediction file invalid: {len(errors)} error(s)")

    python_bin = SWEBENCH_VENV / "bin" / "python"
    if not python_bin.exists():
        raise FileNotFoundError(
            f"SWE-bench venv not found at {SWEBENCH_VENV}. "
            "Install swebench there or set the SWEBENCH_VENV env var."
        )

    cmd = [
        str(python_bin), "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--predictions_path", str(predictions_path.resolve()),
        "--max_workers", str(max_workers),
        "--run_id", run_id,
        "--namespace", "",
    ]

    if instance_ids:
        cmd.extend(["--instance_ids"] + instance_ids)

    logger.info("Running SWE-bench evaluation: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=str(SWEBENCH_WORK_DIR),
        text=True,
        capture_output=True,
        timeout=timeout * max(len(instance_ids or [1]), 1) * 5 + 300,
    )

    if result.stdout:
        logger.info("Harness stdout:\n%s", result.stdout[-2000:])
    if result.stderr:
        logger.info("Harness stderr:\n%s", result.stderr[-2000:])

    if result.returncode != 0:
        logger.error("Harness exited with code %d", result.returncode)
    else:
        logger.info("Harness evaluation completed successfully.")

    return result.returncode
