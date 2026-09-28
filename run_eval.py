#!/usr/bin/env python3

import argparse
import logging

import yaml

from claw_swebench.config import CONFIG_DIR, get_predictions_path
from claw_swebench.evaluate import run_evaluation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Evaluate SWE-bench predictions")
    parser.add_argument("--run_id", required=True,
                        help="Run identifier used by run_infer.py (also the evaluation run ID)")
    parser.add_argument("--dataset", choices=["verified", "multilingual"], default=None,
                        help="Which benchmark the run used (loads config/<dataset>.yaml)")
    parser.add_argument("--predictions", default=None,
                        help="Predictions JSONL (default: artifacts/<run_id>/predictions.jsonl)")
    parser.add_argument("--dataset_name", default=None,
                        help="Full HF dataset name (default: from config/<dataset>.yaml)")
    parser.add_argument("--instance_ids", nargs="+", default=None,
                        help="Specific instance IDs to evaluate")
    parser.add_argument("--max_workers", type=int, default=1,
                        help="Number of parallel workers (default: 1)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Timeout per instance in seconds (default: 1800)")
    args = parser.parse_args()

    dataset_name = args.dataset_name
    if dataset_name is None:
        if args.dataset is None:
            parser.error("one of --dataset or --dataset_name is required")
        with open(CONFIG_DIR / f"{args.dataset}.yaml") as f:
            dataset_name = yaml.safe_load(f)["dataset_name"]
    predictions = args.predictions or get_predictions_path(args.run_id)

    rc = run_evaluation(
        predictions_path=predictions,
        dataset_name=dataset_name,
        run_id=args.run_id,
        instance_ids=args.instance_ids,
        max_workers=args.max_workers,
        timeout=args.timeout,
    )

    if rc == 0:
        logger.info("Evaluation finished successfully.")
    else:
        logger.error("Evaluation failed with exit code %d", rc)
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
