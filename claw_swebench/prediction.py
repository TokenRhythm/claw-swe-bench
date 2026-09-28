
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def format_prediction(instance_id: str, patch: str, model_name: str) -> dict:
    return {
        "instance_id": instance_id,
        "model_patch": patch,
        "model_name_or_path": model_name,
    }


def write_predictions(predictions: list[dict], output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")

    logger.info("Wrote %d predictions to %s", len(predictions), output_path)


def append_prediction(prediction: dict, output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "a") as f:
        f.write(json.dumps(prediction, ensure_ascii=False) + "\n")


def validate_prediction_file(path: str | Path, expected_ids: set[str] | None = None) -> list[str]:
    path = Path(path)
    errors = []

    if not path.exists():
        return [f"File not found: {path}"]

    if path.stat().st_size == 0:
        return [f"File is empty: {path}"]

    seen_ids = set()
    line_count = 0

    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            line_count += 1

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"Line {i}: invalid JSON: {e}")
                continue

            for field in ("instance_id", "model_patch", "model_name_or_path"):
                if field not in entry:
                    errors.append(f"Line {i}: missing field '{field}'")

            instance_id = entry.get("instance_id", "")
            if instance_id in seen_ids:
                errors.append(f"Line {i}: duplicate instance_id '{instance_id}'")
            seen_ids.add(instance_id)

    if line_count == 0:
        errors.append("No prediction entries found")

    if expected_ids:
        missing = expected_ids - seen_ids
        if missing:
            errors.append(f"Missing {len(missing)} instance(s): {sorted(missing)[:5]}...")

    if not errors:
        logger.info("Prediction file valid: %d entries in %s", line_count, path)

    return errors
