
import logging
from pathlib import Path

from datasets import load_dataset

from claw_swebench.config import DEFAULT_SPLIT

logger = logging.getLogger(__name__)


def load_instances(
    dataset_name: str,
    split: str = DEFAULT_SPLIT,
    instance_ids: list[str] | None = None,
    instance_file: str | None = None,
) -> list[dict]:
    filter_ids = set(instance_ids or [])
    if instance_file:
        path = Path(instance_file)
        if not path.exists():
            raise FileNotFoundError(f"Instance file not found: {path}")
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                filter_ids.add(line)

    logger.info("Loading dataset %s (split=%s)...", dataset_name, split)
    ds = load_dataset(dataset_name, split=split)
    logger.info("Loaded %d instances from dataset.", len(ds))

    instances = [dict(row) for row in ds]

    if filter_ids:
        instances = [i for i in instances if i["instance_id"] in filter_ids]
        logger.info("Filtered to %d instances.", len(instances))
        missing = filter_ids - {i["instance_id"] for i in instances}
        if missing:
            logger.warning("Requested but not found in dataset: %s", missing)

    return instances
