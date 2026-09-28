
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from claw_swebench.config import get_artifact_dir, get_predictions_path, get_state_path
from claw_swebench.patch import clean_patch, collect_patch, is_empty_patch
from claw_swebench.prediction import append_prediction, format_prediction
from claw_swebench.prompt import build_prompt
from claw_swebench.types import AgentResult, InstanceRecord, InstanceState
from claw_swebench.workspace import SWEBenchWorkspace

logger = logging.getLogger(__name__)


def _save_metadata(
    artifact_dir: Path,
    record: InstanceRecord,
    agent_result: AgentResult | None,
    extra_usage: dict | None = None,
) -> None:
    data = {
        "instance_id": record.instance_id,
        "state": record.state.value,
        "model": record.model,
        "run_id": record.run_id,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_seconds": record.duration_seconds,
        "patch_empty": record.patch_empty,
        "error": record.error,
    }
    if agent_result:
        data["agent"] = {
            "success": agent_result.success,
            "finish_reason": agent_result.finish_reason,
            "exit_code": agent_result.exit_code,
            "duration_seconds": agent_result.duration_seconds,
            "session_id": agent_result.session_id,
            "usage": agent_result.usage,
        }
    if extra_usage:
        data["usage"] = extra_usage
    (artifact_dir / "metadata.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _append_state(state_path: Path, record: InstanceRecord) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "instance_id": record.instance_id,
        "state": record.state.value,
        "model": record.model,
        "run_id": record.run_id,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "duration_seconds": record.duration_seconds,
        "patch_empty": record.patch_empty,
        "error": record.error,
    }
    with open(state_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_completed_ids(state_path: Path) -> set[str]:
    completed = set()
    if not state_path.exists():
        return completed
    with open(state_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("state") in (
                    InstanceState.PATCH_COLLECTED.value,
                    InstanceState.EVAL_DONE.value,
                ):
                    completed.add(entry["instance_id"])
            except json.JSONDecodeError:
                continue
    return completed


def run_one_instance(
    instance: dict,
    adapter,
    model_name: str,
    run_id: str,
    setup_gitignore: bool = False,
    file_lock=None,
) -> InstanceRecord:
    instance_id = instance["instance_id"]
    base_commit = instance["base_commit"]
    artifact_dir = get_artifact_dir(run_id, instance_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = get_predictions_path(run_id)
    state_path = get_state_path(run_id)

    record = InstanceRecord(
        instance_id=instance_id,
        state=InstanceState.RUNNING,
        model=model_name,
        run_id=run_id,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    workspace = SWEBenchWorkspace(instance_id, adapter)
    agent_id = f"swe-{instance_id}".replace(".", "-")
    agent_result = None
    extra_usage: dict = {}

    try:
        adapter.create_agent(agent_id)

        container_name = workspace.start()
        workspace.prepare_instance(base_commit, setup_gitignore=setup_gitignore)

        prompt = build_prompt(instance, template_path=adapter.prompt_template())
        (artifact_dir / "prompt.txt").write_text(prompt)

        logger.info("Sending task to %s for %s (agent=%s)...",
                    adapter.name, instance_id, agent_id)
        agent_result = adapter.send_task(
            prompt,
            agent_id=agent_id,
            container_name=container_name,
            artifact_dir=artifact_dir,
            instance_id=instance_id,
        )
        logger.info(
            "Agent finished: success=%s finish=%s duration=%.1fs",
            agent_result.success, agent_result.finish_reason, agent_result.duration_seconds,
        )

        extra_usage = adapter.collect_usage(workspace, artifact_dir) or {}

        raw_patch = collect_patch(workspace, base_commit)
        cleaned = clean_patch(raw_patch)
        patch_empty = is_empty_patch(cleaned)

        (artifact_dir / "git.patch").write_text(cleaned if not patch_empty else "")
        if raw_patch != cleaned:
            (artifact_dir / "git.patch.raw").write_text(raw_patch)

        adapter.backup_session(agent_id, artifact_dir)

        prediction = format_prediction(instance_id, cleaned, model_name)
        if file_lock:
            with file_lock:
                append_prediction(prediction, predictions_path)
        else:
            append_prediction(prediction, predictions_path)

        record.state = InstanceState.PATCH_COLLECTED
        record.patch_empty = patch_empty
        if agent_result.timeout:
            record.state = InstanceState.TIMEOUT
        elif not agent_result.success and patch_empty:
            record.state = InstanceState.FAILED
            record.error = f"agent_finish_reason={agent_result.finish_reason}"

    except Exception as e:
        logger.error("Instance %s failed: %s", instance_id, e)
        record.state = InstanceState.FAILED
        record.error = str(e)

    finally:
        adapter.delete_agent(agent_id)
        workspace.cleanup()
        record.finished_at = datetime.now(timezone.utc).isoformat()
        if record.started_at and record.finished_at:
            start = datetime.fromisoformat(record.started_at)
            end = datetime.fromisoformat(record.finished_at)
            record.duration_seconds = round((end - start).total_seconds(), 1)

        _save_metadata(artifact_dir, record, agent_result, extra_usage)
        if file_lock:
            with file_lock:
                _append_state(state_path, record)
        else:
            _append_state(state_path, record)

    return record


def run_batch(
    instances: list[dict],
    adapter,
    model_name: str,
    run_id: str,
    setup_gitignore: bool = False,
    resume: bool = True,
    max_workers: int = 1,
) -> list[InstanceRecord]:
    state_path = get_state_path(run_id)
    completed_ids = _load_completed_ids(state_path) if resume else set()

    if completed_ids:
        logger.info("Resuming: %d instances already completed.", len(completed_ids))

    to_run = []
    for i, instance in enumerate(instances):
        instance_id = instance["instance_id"]
        if instance_id in completed_ids:
            logger.info("[%d/%d] Skipping %s (already completed)", i + 1, len(instances), instance_id)
        else:
            to_run.append(instance)

    total = len(to_run)
    if total == 0:
        logger.info("No instances to run.")
        return []

    logger.info("Running %d instances with %d worker(s).", total, max_workers)

    records = []

    if max_workers <= 1:
        for i, instance in enumerate(to_run):
            logger.info("[%d/%d] Running %s...", i + 1, total, instance["instance_id"])
            record = run_one_instance(
                instance=instance,
                adapter=adapter,
                model_name=model_name,
                run_id=run_id,
                setup_gitignore=setup_gitignore,
            )
            records.append(record)
            logger.info(
                "[%d/%d] %s → %s (%.1fs, patch_empty=%s)",
                i + 1, total, instance["instance_id"], record.state.value,
                record.duration_seconds or 0, record.patch_empty,
            )
    else:
        file_lock = threading.Lock()
        completed_count = 0
        count_lock = threading.Lock()

        def _run_and_log(instance: dict) -> InstanceRecord:
            nonlocal completed_count
            instance_id = instance["instance_id"]
            logger.info("Running %s...", instance_id)
            record = run_one_instance(
                instance=instance,
                adapter=adapter,
                model_name=model_name,
                run_id=run_id,
                setup_gitignore=setup_gitignore,
                file_lock=file_lock,
            )
            with count_lock:
                completed_count += 1
                logger.info(
                    "[%d/%d] %s → %s (%.1fs, patch_empty=%s)",
                    completed_count, total, instance_id, record.state.value,
                    record.duration_seconds or 0, record.patch_empty,
                )
            return record

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_and_log, inst): inst["instance_id"]
                for inst in to_run
            }
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    record = future.result()
                    records.append(record)
                except Exception as e:
                    logger.error("Instance %s raised exception: %s", instance_id, e)

    states = {}
    for r in records:
        states[r.state.value] = states.get(r.state.value, 0) + 1
    logger.info("Batch complete: %s", states)

    return records
