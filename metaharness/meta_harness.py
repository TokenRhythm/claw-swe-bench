#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_wrapper

_USE_COLOR = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _bold(t):
    return _c("1", t)


def _dim(t):
    return _c("2", t)


def _green(t):
    return _c("32", t)


def _red(t):
    return _c("31", t)


def _yellow(t):
    return _c("33", t)


def _cyan(t):
    return _c("36", t)


def _ts():
    return _dim(datetime.now().strftime("[%H:%M:%S]"))


def _elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _rate_str(rate):
    s = f"{rate:.0%}"
    if rate >= 0.75:
        return _green(s)
    elif rate >= 0.25:
        return _yellow(s)
    return _red(s)


EVOLVE_DIR = Path(__file__).resolve().parent.parent
META_DIR = Path(__file__).resolve().parent
LOGS_DIR = EVOLVE_DIR / "logs"
JOBS_DIR = EVOLVE_DIR / "jobs"
PENDING_EVAL = LOGS_DIR / "pending_eval.json"
FRONTIER_VAL = LOGS_DIR / "frontier_val.json"
EVOLUTION_SUMMARY = LOGS_DIR / "evolution_summary.jsonl"
AGENTS_DIR = EVOLVE_DIR / "agents"
ARTIFACTS_DIR = EVOLVE_DIR / "artifacts"
EVAL_LOG_ROOT = Path(os.environ.get("SWEBENCH_WORK_DIR", "/data")) / "logs" / "run_evaluation"
SKILL_PATH = META_DIR / "SKILL.md"

BASELINES = [
    ("generic-baseline", "baseline_generic"),
]
BASELINE_AGENT_NAME = BASELINES[0][0]

EVAL_TASK_SETS = {
    "full350": [
        ("verified", "princeton-nlp/SWE-bench_Verified", "config/verified_mini_50.txt", "vmini50"),
        ("multilingual", "SWE-bench/SWE-bench_Multilingual", "config/multilingual_300_instances.txt", "multi300"),
    ],
}
EVAL_TASK_SET = "full350"

SMOKE_TEST_TASK = ("multilingual", "rubocop__rubocop-13362")

MODEL = "deepseek/deepseek-v4.1-flash"
LLM_NO = 1
MODEL_DIRNAME = MODEL.replace("/", "__")
NETWORK_WHITELIST = "openrouter.ai"
AGENT_TIMEOUT = 3600
DEFAULT_CONCURRENCY = 8
DEFAULT_EVAL_WORKERS = 4
BENCH_TIMEOUT_SECONDS = int(os.environ.get("BENCH_TIMEOUT_SECONDS", 24 * 60 * 60))
PRICE_PER_M = (0.30, 0.006, 1.20)

FROZEN_LOCAL = ["claw_swebench", "claw_configs/generic", "config", "prompts", "run_infer.py", "run_eval.py",
                "agents/baseline_generic", "metaharness/meta_harness.py", "metaharness/hermes_wrapper.py",
                "metaharness/SKILL.md"]
FROZEN_EXTERNAL = {
    Path(os.environ.get("GA_REPO_PATH", "/opt/genericagent")): ["agentmain.py", "agent_loop.py", "ga.py", "llmcore.py",
                                                                "assets", "plugins", "memory"],
}

_interrupted = False


def _handle_signal(signum, frame):
    global _interrupted
    _interrupted = True
    print("\nInterrupted, finishing current step...", flush=True)


def run_cmd(cmd, timeout=BENCH_TIMEOUT_SECONDS, cwd=None, log_path=None):
    env = os.environ.copy()
    env["SWE_NETWORK_WHITELIST"] = NETWORK_WHITELIST
    try:
        if log_path:
            with open(log_path, "a") as fh:
                fh.write(f"\n$ {' '.join(map(str, cmd))}\n")
                fh.flush()
                p = subprocess.run(cmd, cwd=cwd, timeout=timeout, stdout=fh, stderr=subprocess.STDOUT, env=env)
            return subprocess.CompletedProcess(cmd, p.returncode, "", "")
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"Timed out after {timeout}s")


def _hash_tree(root, rels):
    h = {}
    for rel in rels:
        p = root / rel
        if not p.exists():
            continue
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts)
        for f in files:
            h[f"{root}/{f.relative_to(root)}"] = hashlib.md5(f.read_bytes()).hexdigest()
    return h


def frozen_hashes():
    h = _hash_tree(EVOLVE_DIR, FROZEN_LOCAL)
    for root, rels in FROZEN_EXTERNAL.items():
        h.update(_hash_tree(root, rels))
    return h


def frozen_check(baseline, stage):
    cur = frozen_hashes()
    diff = sorted(k for k in set(baseline) | set(cur) if baseline.get(k) != cur.get(k))
    if diff:
        print(f"{_red('ABORT')} frozen files changed ({stage}):")
        for d in diff[:20]:
            print(f"    {d}")
        raise SystemExit(2)


REQUIRED_FILES = ("agentmain.py", "agent_loop.py", "ga.py", "llmcore.py", "assets/tools_schema.json",
                  "assets/sys_prompt_en.txt", "memory")

FORBIDDEN_PATTERNS = (r"git\s+log\s+--all", r"git\s+reflog", r"git\s+branch\s+-a", r"git\s+fetch", r"git\s+pull",
                      r"\bcurl\s", r"\bwget\s", r"pip\s+install", r"\bgit\s+clone")


_NEGATION_RE = re.compile(r"\b(do not|don't|never|not|avoid|no)\b", re.I)


def _forbidden_hits(text, patterns):
    hard, soft = [], []
    lines = text.splitlines()
    for pat in patterns:
        for i, ln in enumerate(lines):
            if re.search(pat, ln):
                ctx = " ".join(lines[max(0, i - 2):i + 1])
                (soft if _NEGATION_RE.search(ctx) else hard).append((pat, ln.strip()[:100]))
                break
    return hard, soft


def validate_agent_dir(agent_dir_name):
    agent_dir = AGENTS_DIR / agent_dir_name
    if not agent_dir.is_dir():
        raise FileNotFoundError(f"agents/{agent_dir_name} is not a directory")
    for rel in REQUIRED_FILES:
        if not (agent_dir / rel).exists():
            raise FileNotFoundError(f"agents/{agent_dir_name} is missing {rel}")
    import ast
    for py in sorted(agent_dir.rglob("*.py")):
        if "__pycache__" in py.parts or "/temp/" in str(py):
            continue
        try:
            ast.parse(py.read_text(errors="replace"), filename=str(py))
        except SyntaxError as exc:
            raise SyntaxError(f"{py.relative_to(agent_dir)}: {exc}") from None
    json.loads((agent_dir / "assets/tools_schema.json").read_text())
    main_src = (agent_dir / "agentmain.py").read_text()
    for must in ("--task", "--llm_no", "--nobg", "[ROUND END]"):
        if must not in main_src:
            raise ValueError(f"agentmain.py lost the task-mode contract token {must!r}")
    for py in list(agent_dir.rglob("*.py")) + list((agent_dir / "assets").glob("*.txt")) \
            + list((agent_dir / "assets").glob("*.json")) + list((agent_dir / "memory").rglob("*")):
        if not py.is_file() or "__pycache__" in py.parts:
            continue
        text = py.read_text(errors="replace")
        for pat in FORBIDDEN_PATTERNS:
            if re.search(pat, text):
                base = AGENTS_DIR / BASELINES[0][1] / py.relative_to(agent_dir)
                base_text = base.read_text(errors="replace") if base.exists() else ""
                new_lines = set(text.splitlines()) - set(base_text.splitlines())
                hard, _soft = _forbidden_hits("\n".join(new_lines), (pat,))
                if hard:
                    raise ValueError(f"{py.relative_to(agent_dir)}: forbidden pattern /{pat}/ added: {hard[0][1]}")
    return agent_dir


def load_task_ids(task_set):
    ids = []
    for _, _, inst_file, _ in EVAL_TASK_SETS[task_set]:
        ids += [ln.strip() for ln in (EVOLVE_DIR / inst_file).read_text().splitlines() if ln.strip()]
    return ids


def audit_task_specific(agent_dir, task_ids):
    toks = set()
    for iid in task_ids:
        owner, _, repo = iid.rsplit("-", 1)[0].partition("__")
        toks.update(t.lower() for t in (owner, repo) if len(t) >= 4)
    toks -= {"framework", "python", "core", "apache", "google", "microsoft", "facebook", "spring", "tools",
             "test", "tests", "docs", "main", "http", "json", "django"}
    base_dir = AGENTS_DIR / BASELINES[0][1]
    hits = []
    for f in sorted(agent_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix not in (".py", ".txt", ".json", ".md"):
            continue
        base = base_dir / f.relative_to(agent_dir)
        base_lines = set(base.read_text(errors="replace").splitlines()) if base.exists() else set()
        for ln in f.read_text(errors="replace").splitlines():
            if ln in base_lines:
                continue
            low = ln.lower()
            for iid in task_ids:
                if iid.lower() in low:
                    hits.append(f"{f.relative_to(agent_dir)}: instance id {iid}")
            for t in toks:
                if re.search(r"(?<![a-z0-9_])" + re.escape(t) + r"(?![a-z0-9_])", low):
                    hits.append(f"{f.relative_to(agent_dir)}: repo token '{t}': {ln.strip()[:80]}")
    return hits


def _run_namespace():
    namespace = os.path.relpath(JOBS_DIR, EVOLVE_DIR / "jobs")
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", namespace).strip("-")
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:12]
    return f"{label}-{digest}"


def swebench_run(agent_dir_name, job_name, task_set=EVAL_TASK_SET, n_concurrent=DEFAULT_CONCURRENCY,
                 eval_workers=DEFAULT_EVAL_WORKERS, instance_ids=None):
    job_dir = JOBS_DIR / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    run_ids = []
    evals = []
    ok = True
    for dataset, dataset_name, inst_file, tag in EVAL_TASK_SETS[task_set]:
        if instance_ids is not None:
            split_ids = [i for i in instance_ids if i in set(load_task_ids(task_set)) and
                         i in set((EVOLVE_DIR / inst_file).read_text().split())]
            if not split_ids:
                continue
        # The named evolution run owns these artifacts; retries resume across dates.
        run_id = f"mh-gen-{_run_namespace()}-{job_name}-{tag}"
        run_ids.append(run_id)
        cmd = [sys.executable, "run_infer.py", "--claw", "generic", "--dataset", dataset, "--model", MODEL,
               "--llm_no", str(LLM_NO), "--candidate", str(AGENTS_DIR / agent_dir_name), "--run_id", run_id,
               "--timeout", str(AGENT_TIMEOUT), "--workers", str(n_concurrent)]
        cmd += ["--instance_ids", *split_ids] if instance_ids is not None else ["--instance_file", inst_file]
        r = run_cmd(cmd, cwd=str(EVOLVE_DIR), log_path=job_dir / f"infer-{tag}.log")
        if r.returncode != 0:
            print(f"  {_red('infer failed')} exit={r.returncode} run={run_id}")
            ok = False
        preds = ARTIFACTS_DIR / run_id / "predictions.jsonl"
        if not preds.exists():
            print(f"  {_yellow('no predictions')} run={run_id}")
            continue
        elog = open(job_dir / f"eval-{tag}.log", "a")
        env = os.environ.copy()
        p = subprocess.Popen([sys.executable, "run_eval.py", "--dataset", dataset, "--run_id", run_id,
                              "--max_workers", str(eval_workers)],
                             cwd=str(EVOLVE_DIR), stdout=elog, stderr=subprocess.STDOUT, env=env)
        evals.append((p, elog, run_id))
    for p, elog, run_id in evals:
        p.wait()
        elog.close()
        if p.returncode != 0:
            print(f"  {_red('eval failed')} exit={p.returncode} run={run_id}")
            ok = False
    (job_dir / "job.json").write_text(json.dumps({
        "agent_dir": agent_dir_name, "task_set": task_set, "model": MODEL, "llm_no": LLM_NO,
        "run_ids": run_ids, "n_attempts": 1, "finished": datetime.now().isoformat(), "ok": ok,
    }, indent=2))
    return job_dir, ok


def _job_run_ids(job_dir):
    jf = job_dir / "job.json"
    if not jf.exists():
        return []
    return json.loads(jf.read_text()).get("run_ids", [])


def _artifact_root(run_id):
    p = Path(run_id)
    return p if p.is_absolute() else ARTIFACTS_DIR / run_id


def parse_job_results(job_dir, expected_trials=1, task_ids=None):
    task_rewards = {}
    task_ids = set(task_ids) if task_ids else None
    for run_id in _job_run_ids(job_dir):
        art = _artifact_root(run_id)
        state = art / "state.jsonl"
        if state.exists():
            for line in state.read_text().splitlines():
                try:
                    iid = json.loads(line)["instance_id"]
                except (json.JSONDecodeError, KeyError):
                    continue
                if task_ids is None or iid in task_ids:
                    task_rewards.setdefault(iid, [0])
        ev = EVAL_LOG_ROOT / Path(run_id).name / MODEL_DIRNAME
        for rep in ev.glob("*/report.json"):
            try:
                for iid, v in json.loads(rep.read_text()).items():
                    if task_ids is None or iid in task_ids:
                        task_rewards[iid] = [1 if v.get("resolved") else 0]
            except json.JSONDecodeError:
                continue
    return task_rewards


def compute_pass_rates(task_rewards):
    per_task = {t: sum(r) / len(r) for t, r in task_rewards.items() if r}
    total = sum(sum(r) for r in task_rewards.values())
    n = sum(len(r) for r in task_rewards.values())
    return per_task, (total / n if n else 0.0)


def parse_trial_metrics(job_dir, task_ids=None):
    per_task = {}
    task_ids = set(task_ids) if task_ids else None
    for run_id in _job_run_ids(job_dir):
        art = _artifact_root(run_id)
        for meta in art.glob("*/metadata.json"):
            iid = meta.parent.name
            if task_ids is not None and iid not in task_ids:
                continue
            try:
                md = json.loads(meta.read_text())
            except json.JSONDecodeError:
                continue
            ag = md.get("agent") or {}
            us = ag.get("usage") or {}
            n_in, n_cache, n_out = us.get("input"), us.get("cacheRead"), us.get("output")
            cost = None
            if n_in is not None and n_out is not None:
                cost = ((n_in - (n_cache or 0)) * PRICE_PER_M[0] + (n_cache or 0) * PRICE_PER_M[1]
                        + n_out * PRICE_PER_M[2]) / 1e6
            n_calls = None
            so = meta.parent / "agent_stdout.log"
            if so.exists():
                n_calls = sum(1 for ln in so.read_text(errors="replace").splitlines() if ln.startswith("[Cache] input="))
            per_task.setdefault(iid, []).append({
                "n_input_tokens": n_in, "n_output_tokens": n_out, "n_cache_tokens": n_cache,
                "cost_usd": round(cost, 4) if cost is not None else None,
                "n_turns": n_calls, "n_api_calls": n_calls,
                "duration_s": md.get("duration_seconds"), "state": md.get("state"),
                "patch_empty": md.get("patch_empty"), "finish_reason": ag.get("finish_reason"),
                "run_id": Path(run_id).name,
            })
    return per_task


def summarize_trial_metrics(trial_metrics):
    all_costs, all_input, all_output, all_cache, all_turns, all_dur = [], [], [], [], [], []
    per_task_summary = {}
    n_timeout = n_failed = n_empty = 0
    for task, trials in trial_metrics.items():
        task_costs = [t["cost_usd"] for t in trials if t["cost_usd"] is not None]
        task_turns = [t["n_turns"] for t in trials if t["n_turns"] is not None]
        per_task_summary[task] = {
            "mean_cost": round(sum(task_costs) / len(task_costs), 3) if task_costs else None,
            "mean_turns": round(sum(task_turns) / len(task_turns), 1) if task_turns else None,
            "n_trials": len(trials),
            "state": trials[-1]["state"],
            "duration_s": trials[-1]["duration_s"],
        }
        all_costs.extend(task_costs)
        all_turns.extend(task_turns)
        all_input.extend(t["n_input_tokens"] for t in trials if t["n_input_tokens"] is not None)
        all_output.extend(t["n_output_tokens"] for t in trials if t["n_output_tokens"] is not None)
        all_cache.extend(t["n_cache_tokens"] for t in trials if t["n_cache_tokens"] is not None)
        all_dur.extend(t["duration_s"] for t in trials if t["duration_s"] is not None)
        n_timeout += sum(1 for t in trials if t["state"] == "timeout")
        n_failed += sum(1 for t in trials if t["state"] == "failed")
        n_empty += sum(1 for t in trials if t["patch_empty"])
    n_trials = sum(len(v) for v in trial_metrics.values())
    return {
        "n_trials": n_trials,
        "total_cost_usd": round(sum(all_costs), 2) if all_costs else None,
        "mean_cost_usd": round(sum(all_costs) / len(all_costs), 3) if all_costs else None,
        "total_input_tokens": sum(all_input) if all_input else None,
        "total_output_tokens": sum(all_output) if all_output else None,
        "total_cache_tokens": sum(all_cache) if all_cache else None,
        "mean_turns": round(sum(all_turns) / len(all_turns), 1) if all_turns else None,
        "mean_duration_s": round(sum(all_dur) / len(all_dur), 1) if all_dur else None,
        "n_timeout": n_timeout, "n_failed": n_failed, "n_empty_patch": n_empty,
        "per_task": per_task_summary,
    }


def count_iterations():
    if not EVOLUTION_SUMMARY.exists():
        return 0
    max_iter = 0
    for line in EVOLUTION_SUMMARY.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            max_iter = max(max_iter, json.loads(line).get("iteration", 0))
        except json.JSONDecodeError:
            continue
    return max_iter


def update_frontier(candidates_results):
    frontier = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}

    for agent_name, (per_task, avg) in candidates_results.items():
        for task, rate in per_task.items():
            current_best = frontier.get(task, {}).get("pass_rate", -1)
            if rate > current_best:
                frontier[task] = {
                    "best_agent": agent_name,
                    "pass_rate": rate,
                }

        current_best_avg = frontier.get("_best", {}).get("avg_pass_rate", -1)
        if avg > current_best_avg:
            frontier["_best"] = {
                "agent": agent_name,
                "avg_pass_rate": avg,
            }

    FRONTIER_VAL.write_text(json.dumps(frontier, indent=2))


def update_evolution_summary(iteration, candidates, results, propose_time=None, bench_time=None, metrics=None,
                             extra=None):
    frontier = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}
    best_avg = frontier.get("_best", {}).get("avg_pass_rate", 0)
    metrics = metrics or {}
    extra = extra or {}

    with open(EVOLUTION_SUMMARY, "a") as f:
        for i, c in enumerate(candidates):
            name = c["name"]
            per_task, avg = results.get(name, ({}, 0))
            row = {
                "iteration": iteration,
                "agent": name,
                "agent_dir": c.get("agent_dir", ""),
                "avg_pass_rate": round(avg, 3),
                "n_resolved": int(round(sum(per_task.values()))),
                "n_tasks": len(per_task),
                "per_task": {k: round(v, 3) for k, v in per_task.items()},
                "hypothesis": c.get("hypothesis", ""),
                "changes": c.get("changes", ""),
                "delta": round(avg - best_avg, 3) if best_avg else None,
                "outcome": f"{avg:.1%} ({avg - best_avg:+.1%})" if avg > 0 else "failed",
            }
            if i == 0 and propose_time is not None:
                row["timing_s"] = {
                    "propose": round(propose_time, 1),
                    "bench": round(bench_time, 1) if bench_time else None,
                }
            if name in metrics:
                row["rollout_metrics"] = metrics[name]
            row.update(extra.get(name, {}))
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def propose_hermes(task_prompt, iteration, timeout=2400, max_turns=300):
    os.environ.pop("CLAUDECODE", None)
    result = hermes_wrapper.run(
        prompt=task_prompt,
        cwd=str(EVOLVE_DIR),
        log_dir=str(LOGS_DIR / "proposer_sessions"),
        name=f"iter{iteration}",
        timeout_seconds=timeout,
        skills=[str(SKILL_PATH)],
        max_turns=max_turns,
    )
    if result.exit_code != 0:
        print(f"  {_red('proposer failed')} exit={result.exit_code}")
        if result.stderr:
            print(f"  {_dim(result.stderr[-500:])}")
        return PENDING_EVAL.exists(), result
    result.show()
    return PENDING_EVAL.exists(), result


def validate_candidate(name, agent_dir_name):
    result = run_cmd(
        [sys.executable, str(Path(__file__)), "--validate-agent", agent_dir_name],
        cwd=str(EVOLVE_DIR),
        timeout=120,
    )
    if result.returncode == 0 and "OK" in result.stdout:
        return True
    print(f"  {_red('validate FAIL')}: {name}")
    if result.stderr:
        print(f"    {_dim(result.stderr[-300:])}")
    return False


def smoke_test(name, agent_dir_name, timeout=AGENT_TIMEOUT + 600):
    job_name = f"smoke-{name}"
    job_dir = JOBS_DIR / job_name
    if job_dir.exists():
        shutil.rmtree(job_dir)
    job_dir.mkdir(parents=True)
    dataset, iid = SMOKE_TEST_TASK
    # Smoke disables resume, so each invocation needs a new artifact directory.
    run_id = f"mh-gen-{_run_namespace()}-{job_name}-{dataset}-{uuid4().hex}"
    t0 = time.time()
    result = run_cmd(
        [sys.executable, "run_infer.py", "--claw", "generic", "--dataset", dataset, "--model", MODEL,
         "--llm_no", str(LLM_NO), "--candidate", str(AGENTS_DIR / agent_dir_name), "--instance_ids", iid,
         "--run_id", run_id, "--timeout", str(AGENT_TIMEOUT),
         "--workers", "1", "--no_resume"],
        timeout=timeout, cwd=str(EVOLVE_DIR), log_path=job_dir / "infer-smoke.log",
    )
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"  {_red('smoke FAIL')}: {name} exit={result.returncode} ({_elapsed(elapsed)})")
        return False

    result_file = ARTIFACTS_DIR / run_id / iid / "metadata.json"
    if not result_file.exists():
        print(f"  {_red('smoke FAIL')}: {name} (no metadata.json, {_elapsed(elapsed)})")
        return False

    data = json.loads(result_file.read_text())
    agent = data.get("agent") or {}
    if (data.get("state") != "patch_collected" or data.get("patch_empty") is not False
            or agent.get("success") is not True or agent.get("finish_reason") != "stop"
            or agent.get("timeout")):
        print(f"  {_red('smoke FAIL')}: {name} (state={data.get('state')} exit={agent.get('exit_code')} "
              f"patch_empty={data.get('patch_empty')} err={str(data.get('error'))[:120]}, {_elapsed(elapsed)})")
        return False

    (job_dir / "job.json").write_text(json.dumps({"agent_dir": agent_dir_name, "run_ids": [run_id], "smoke": True}))
    print(f"  {_green('smoke OK')}: {name} ({_elapsed(elapsed)}, state={data.get('state')} "
          f"exit={agent.get('exit_code')})")
    return True


def render_task_prompt(iteration, task_set, task_ids):
    frontier = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}
    best = frontier.get("_best", {})
    unresolved = [t for t in task_ids if frontier.get(t, {}).get("pass_rate", 0) == 0]
    job_lines = []
    for jd in sorted(JOBS_DIR.iterdir()) if JOBS_DIR.exists() else []:
        for rid in _job_run_ids(jd):
            job_lines.append(f"- job `{jd.name}`: artifacts `{_artifact_root(rid)}/<instance_id>/` "
                             f"(agent_stdout.log = full tool-call trace, input.txt, output.txt, git.patch, metadata.json); "
                             f"grading `{EVAL_LOG_ROOT / Path(rid).name / MODEL_DIRNAME}/<instance_id>/` "
                             f"(report.json, test_output.txt)")
    return (
        f"Run iteration {iteration} of the scaffold evolution loop (GenericAgent track). "
        f"Model: {MODEL} (reasoning_effort xhigh via OpenRouter; frozen). "
        f"Start from agents/{BASELINES[0][1]}/ as the parent (or the current frontier agent).\n\n"
        f"## Eval split: {task_set} = {len(task_ids)} SWE-bench instances x 1 trial\n"
        f"Instance lists: " + ", ".join(str(EVOLVE_DIR / f) for _, _, f, _ in EVAL_TASK_SETS[task_set]) + "\n\n"
        f"Current frontier: `{best.get('agent', 'none')}` @ {best.get('avg_pass_rate', 0):.1%}. "
        f"Instances NOT resolved by any agent so far ({len(unresolved)}): {', '.join(unresolved)}\n\n"
        f"## Run directories\n"
        f"All logs and results for this run are under `{LOGS_DIR}/`.\n"
        f"- `{EVOLUTION_SUMMARY}` — past results (per_task, hypothesis, rollout_metrics)\n"
        f"- `{FRONTIER_VAL}` — frontier\n"
        f"- Raw traces per job:\n" + "\n".join(job_lines) + "\n"
        f"- Existing agents: " + ", ".join(sorted(p.name for p in AGENTS_DIR.iterdir() if p.is_dir())) + "\n"
        f"- Write pending_eval.json to: `{PENDING_EVAL}`\n\n"
        f"Working directory: {EVOLVE_DIR}. Do not run run_infer.py / run_eval.py / docker yourself."
    )


def fresh_start():
    if AGENTS_DIR.exists():
        for f in AGENTS_DIR.iterdir():
            if f.name in ("__pycache__", "baseline_generic"):
                continue
            if f.is_dir():
                shutil.rmtree(f)
                print(f"  Cleared {f.name}/")

    for f in [EVOLUTION_SUMMARY, FRONTIER_VAL, PENDING_EVAL]:
        if f.exists():
            f.unlink()

    print("  Fresh start: cleared generated agents/ dirs and log files")


def run_evolve(args):
    global JOBS_DIR, LOGS_DIR, PENDING_EVAL, FRONTIER_VAL, EVOLUTION_SUMMARY, EVAL_TASK_SET

    if args.run_name:
        run_name = args.run_name
    else:
        run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    JOBS_DIR = EVOLVE_DIR / "jobs" / run_name
    LOGS_DIR = EVOLVE_DIR / "logs" / run_name
    PENDING_EVAL = LOGS_DIR / "pending_eval.json"
    FRONTIER_VAL = LOGS_DIR / "frontier_val.json"
    EVOLUTION_SUMMARY = LOGS_DIR / "evolution_summary.jsonl"
    EVAL_TASK_SET = args.task_set

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.fresh:
        fresh_start()

    task_ids = load_task_ids(EVAL_TASK_SET)
    n_tasks = len(task_ids)
    print(
        f"{_ts()} {_bold('Evolution (GenericAgent track)')}  run={_cyan(run_name)}  model={_cyan(MODEL)}  "
        f"iters={args.iterations}  trials=1  tasks={n_tasks} ({EVAL_TASK_SET})"
    )

    frozen_path = LOGS_DIR / "frozen_hashes.json"
    if not frozen_path.exists():
        frozen_path.write_text(json.dumps(frozen_hashes(), indent=1))
    frozen = json.loads(frozen_path.read_text())
    frozen_check(frozen, "startup")

    baseline_dirs = {}
    if not args.skip_baseline:
        print(f"\n{_ts()} {_bold('Phase 0: Baselines')}  agents={len(BASELINES)}  trials=1")

    for bl_name, bl_agent_dir in BASELINES:
        bl_job = f"{bl_name}-t1"
        bl_dir = JOBS_DIR / bl_job
        baseline_dirs[bl_name] = bl_dir

        if args.import_baseline and not (bl_dir / "job.json").exists():
            run_ids = [r.strip() for r in args.import_baseline.split(",") if r.strip()]
            bl_dir.mkdir(parents=True, exist_ok=True)
            (bl_dir / "job.json").write_text(json.dumps({
                "agent_dir": bl_agent_dir, "task_set": EVAL_TASK_SET, "model": MODEL, "llm_no": LLM_NO,
                "run_ids": run_ids, "n_attempts": 1, "imported": True, "ok": True}, indent=2))
            print(f"  {_dim('imported')} {bl_name}: {run_ids}")

        if not args.skip_baseline:
            cached_ok = False
            if bl_dir.exists() and parse_job_results(bl_dir, task_ids=task_ids):
                cfg_file = bl_dir / "job.json"
                if cfg_file.exists():
                    try:
                        cfg = json.loads(cfg_file.read_text())
                        if MODEL not in str(cfg.get("model", "")):
                            print(f"  {_yellow('stale')} {bl_name}: model mismatch (cached={cfg.get('model')}, want={MODEL}), re-running")
                        elif cfg.get("n_attempts", 0) != 1:
                            print(f"  {_yellow('stale')} {bl_name}: trials mismatch, re-running")
                        else:
                            cached_ok = True
                    except (json.JSONDecodeError, OSError):
                        pass
                else:
                    cached_ok = True
            if cached_ok:
                print(f"  {_dim('cached')} {bl_name}: {bl_dir}")
            else:
                print(f"  {_ts()} running {_bold(bl_name)}: {n_tasks} tasks x 1 trial...", flush=True)
                t0 = time.time()
                bl_dir, ok = swebench_run(bl_agent_dir, bl_job, task_set=EVAL_TASK_SET,
                                          n_concurrent=args.concurrent, eval_workers=args.eval_workers)
                baseline_dirs[bl_name] = bl_dir
                elapsed = time.time() - t0
                if not ok:
                    print(f"  {_red('FAIL')} {bl_name} crashed after {_elapsed(elapsed)}")
                else:
                    print(f"  {_ts()} {bl_name} completed in {_elapsed(elapsed)}")

    for bl_name, bl_dir in baseline_dirs.items():
        if bl_dir.exists():
            task_rewards = parse_job_results(bl_dir, task_ids=task_ids)
            if task_rewards:
                per_task, avg = compute_pass_rates(task_rewards)
                update_frontier({bl_name: (per_task, avg)})
                if count_iterations() == 0 and not any(
                        json.loads(l).get("agent") == bl_name for l in
                        (EVOLUTION_SUMMARY.read_text().splitlines() if EVOLUTION_SUMMARY.exists() else [])):
                    metrics = summarize_trial_metrics(parse_trial_metrics(bl_dir, task_ids=task_ids))
                    update_evolution_summary(0, [{"name": bl_name, "agent_dir": dict(BASELINES)[bl_name],
                                                  "hypothesis": "baseline (unmodified GenericAgent)"}],
                                             {bl_name: (per_task, avg)}, metrics={bl_name: metrics})
                if not args.skip_baseline:
                    graded = len([t for t in task_ids if t in per_task])
                    print(f"  {_bold(bl_name)}: avg={_rate_str(avg)}  resolved={int(round(sum(per_task.values())))}/{n_tasks}"
                          f"  (graded {graded}/{n_tasks})")

    start_iteration = count_iterations() + 1
    for i in range(args.iterations):
        if _interrupted:
            print("Interrupted.")
            break

        iteration = start_iteration + i
        iter_start = time.time()
        frontier = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}
        best_avg = frontier.get("_best", {}).get("avg_pass_rate", 0)
        best_agent = frontier.get("_best", {}).get("agent", "none")
        print(f"\n{_ts()} {_bold(f'Iteration {iteration}')} ({i + 1}/{args.iterations})  frontier={best_agent} @ {best_avg:.1%}")
        print(f"{'─' * 60}")

        propose_start = time.time()
        task_prompt = render_task_prompt(iteration, EVAL_TASK_SET, task_ids)
        (LOGS_DIR / f"iter{iteration}_task_prompt.md").write_text(task_prompt)
        if args.use_pending:
            ok = PENDING_EVAL.exists()
            proposer_info = {"exit": None, "note": "--use-pending (proposer skipped)"}
            print(f"  {_ts()} {_yellow('using existing pending_eval.json')} (proposer skipped)")
        else:
            if PENDING_EVAL.exists():
                PENDING_EVAL.unlink()
            print(f"  {_ts()} {_cyan('proposing')} new candidates...", flush=True)
            ok, presult = propose_hermes(task_prompt, iteration, timeout=args.propose_timeout, max_turns=args.propose_max_turns)
            proposer_info = {"exit": presult.exit_code, "duration_s": round(presult.duration), "usage": presult.usage,
                             "session_log": presult.log_path}
        propose_time = time.time() - propose_start

        frozen_check(frozen, f"after proposer iteration {iteration}")

        if not ok:
            print(f"  {_red('FAIL')} proposer returned no candidates after {_elapsed(propose_time)}")
            update_evolution_summary(iteration, [{"name": "(none)", "agent_dir": ""}], {}, propose_time=propose_time,
                                     extra={"(none)": {"proposer": proposer_info}})
            continue

        try:
            candidates = json.loads(PENDING_EVAL.read_text()).get("candidates", [])
        except json.JSONDecodeError as exc:
            print(f"  {_red('FAIL')} pending_eval.json unparsable: {exc}")
            continue
        for c in candidates:
            c["name"] = re.sub(r"[^A-Za-z0-9_\-]", "_", str(c.get("name", "")))
            c["agent_dir"] = c.get("agent_dir", c["name"]).split("/")[-1] or c["name"]
        if len(candidates) > 1:
            print(f"  {_yellow('note')} proposer wrote {len(candidates)} candidates; evaluating only the first")
            candidates = candidates[:1]
        print(f"  {_ts()} proposed {len(candidates)} candidate(s) in {_elapsed(propose_time)}")
        for ci, c in enumerate(candidates):
            hyp = c.get("hypothesis", "")
            print(f"    {ci + 1}. {_bold(c['name'])}: {hyp[:80]}")

        valid = []
        audits = {}
        print(f"  {_ts()} {_cyan('validating')} {len(candidates)} candidate(s)...")
        for ci, c in enumerate(candidates):
            name = c["name"]
            agent_dir = c["agent_dir"]
            prefix = f"    [{ci + 1}/{len(candidates)}] {name}:"
            if validate_candidate(name, agent_dir):
                audits[name] = audit_task_specific(AGENTS_DIR / agent_dir, task_ids)
                if audits[name]:
                    print(f"{prefix} {_yellow('audit flags')} {len(audits[name])} (see summary; human review required)")
                if args.skip_smoke:
                    print(f"{prefix} {_green('validate OK')} (smoke skipped)")
                    valid.append(c)
                elif smoke_test(name, agent_dir):
                    print(f"{prefix} {_green('validate OK + smoke OK')}")
                    valid.append(c)
                else:
                    print(f"{prefix} {_red('smoke FAIL')}")
            else:
                print(f"{prefix} {_red('validate FAIL')}")
            if _interrupted:
                break

        if not valid:
            print(f"  {_red('0 valid')} out of {len(candidates)} candidates, skipping iteration")
            update_evolution_summary(iteration, candidates, {}, propose_time=propose_time,
                                     extra={c["name"]: {"proposer": proposer_info, "audit_flags": audits.get(c["name"], [])}
                                            for c in candidates})
            continue
        print(f"  {_green(f'{len(valid)} valid')} out of {len(candidates)} candidates")

        if args.propose_only:
            update_evolution_summary(iteration, valid, {}, propose_time=propose_time,
                                     extra={c["name"]: {"proposer": proposer_info, "audit_flags": audits.get(c["name"], []),
                                                        "note": "propose-only, not benchmarked"} for c in valid})
            print(f"  {_dim('propose-only: skipping benchmark')}")
            continue

        bench_start = time.time()
        results = {}
        all_metrics = {}
        print(f"  {_ts()} {_cyan('benchmarking')} {len(valid)} agent(s) x {n_tasks} tasks x 1 trial")
        for ci, c in enumerate(valid):
            if _interrupted:
                break
            name = c["name"]
            agent_dir = c["agent_dir"]
            job_name = f"evolve-{name}-t1"

            print(f"    [{ci + 1}/{len(valid)}] {_bold(name)}...", flush=True)
            t0 = time.time()
            job_dir, job_result = swebench_run(agent_dir, job_name, task_set=EVAL_TASK_SET,
                                               n_concurrent=args.concurrent, eval_workers=args.eval_workers)
            elapsed = time.time() - t0
            task_rewards = parse_job_results(job_dir, task_ids=task_ids)
            if task_rewards:
                per_task, avg = compute_pass_rates(task_rewards)
                results[name] = (per_task, avg)
                delta = avg - best_avg
                delta_str = f"{delta:+.1%}"
                delta_colored = _green(delta_str) if delta > 0 else (_red(delta_str) if delta < 0 else _dim(delta_str))

                trial_metrics = parse_trial_metrics(job_dir, task_ids=task_ids)
                metrics_summary = summarize_trial_metrics(trial_metrics)
                all_metrics[name] = metrics_summary
                cost_str = f"${metrics_summary['total_cost_usd']:.2f}" if metrics_summary["total_cost_usd"] else "?"
                print(f"         avg={_rate_str(avg)}  resolved={int(round(sum(per_task.values())))}/{n_tasks}  "
                      f"delta={delta_colored}  cost={cost_str}  timeouts={metrics_summary['n_timeout']}  "
                      f"({_elapsed(elapsed)}){'' if job_result else '  ' + _red('[infer/eval reported errors]')}")
                fr = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}
                gained = [t for t, r in per_task.items() if r > fr.get(t, {}).get("pass_rate", 0)]
                lost = [t for t in task_ids if fr.get(t, {}).get("best_agent") == best_agent
                        and fr.get(t, {}).get("pass_rate", 0) > per_task.get(t, 0)]
                print(f"         newly solved vs frontier: {len(gained)}  |  frontier-solved but missed: {len(lost)}")
            else:
                results[name] = ({}, 0)
                print(f"         {_red('FAIL')} benchmark produced no results ({_elapsed(elapsed)})")

        bench_time = time.time() - bench_start

        update_frontier(results)
        update_evolution_summary(iteration, valid, results, propose_time=propose_time, bench_time=bench_time,
                                 metrics=all_metrics,
                                 extra={c["name"]: {"proposer": proposer_info, "audit_flags": audits.get(c["name"], []),
                                                    "job": f"evolve-{c['name']}-t1"} for c in valid})

        wall_time = time.time() - iter_start
        frontier_now = json.loads(FRONTIER_VAL.read_text()) if FRONTIER_VAL.exists() else {}
        new_best_avg = frontier_now.get("_best", {}).get("avg_pass_rate", 0)
        new_best_agent = frontier_now.get("_best", {}).get("agent", "none")
        improved = new_best_avg > best_avg
        status = _green("NEW BEST") if improved else _dim("no improvement")
        print(f"  {_ts()} {status}  frontier={new_best_agent} @ {new_best_avg:.1%}")
        print(f"  {_dim(f'timing: propose={_elapsed(propose_time)} bench={_elapsed(bench_time)} total={_elapsed(wall_time)}')}")

    print(f"\n{_ts()} {_bold('Evolution complete.')}")


def main():
    parser = argparse.ArgumentParser(description="Scaffold evolution loop for SWE-bench (GenericAgent track)")
    parser.add_argument("--iterations", type=int, default=5, help="Number of evolution iterations")
    parser.add_argument("--task-set", choices=EVAL_TASK_SETS.keys(), default=EVAL_TASK_SET,
                        help="Eval split (default: full350 = the 7-harness comparison set)")
    parser.add_argument("--propose-timeout", type=int, default=3600, help="Timeout for proposer (seconds)")
    parser.add_argument("--propose-max-turns", type=int, default=300, help="Hermes max turns for the proposer")
    parser.add_argument("--run-name", type=str, default=None,
                        help="Run name for isolated jobs, logs, and artifact IDs; reuse to resume across dates. "
                             "Auto-generated if not set.")
    parser.add_argument("--fresh", action="store_true", help="Clear proposed agents and reset logs")
    parser.add_argument("--skip-baseline", action="store_true", help="Skip Phase 0 baseline eval")
    parser.add_argument("--import-baseline", type=str, default=None,
                        help="Comma-separated artifacts/<run_id> paths of an existing unmodified-GA run to use as the "
                             "cached baseline (absolute paths allowed)")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip smoke tests")
    parser.add_argument("--use-pending", action="store_true",
                        help="Test hook: skip the proposer and evaluate the candidate already in logs/<run>/pending_eval.json")
    parser.add_argument("--propose-only", action="store_true", help="Propose + validate + smoke, but do not benchmark")
    parser.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Max concurrent instances (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--eval-workers", type=int, default=DEFAULT_EVAL_WORKERS)
    parser.add_argument("--validate-agent", metavar="AGENT_DIR", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.validate_agent:
        try:
            agent_dir = validate_agent_dir(args.validate_agent)
        except Exception as exc:
            print(f"Invalid agent {args.validate_agent}: {exc}", file=sys.stderr)
            return 1
        print(f"OK: {agent_dir}")
        return 0

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    run_evolve(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
