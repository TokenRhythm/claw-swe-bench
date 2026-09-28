# claw-swe-bench

**Leaderboard:** [claw-swe-bench.github.io](https://claw-swe-bench.github.io/)

A unified adapter framework for evaluating agent harnesses ("claws") on
SWE-bench. One command runs any supported claw on
SWE-bench Verified or SWE-bench Multilingual, with identical prompting, patch
collection, and evaluation across all of them — so the harness becomes a
controlled variable and results are comparable harness-to-harness.

This repository is the reference implementation for the technical report
[Claw-SWE-Bench: A Benchmark for Evaluating OpenClaw-Style Agent Harnesses on
Coding Tasks](https://arxiv.org/abs/2606.12344). The full benchmark is 350 GitHub issue-resolution
instances across 8 languages (300 from SWE-bench Multilingual + 50 from
SWE-bench Verified-Mini); the paper also defines an 80-instance **Lite** subset
for low-cost iteration (see *Instance lists* in Notes).


Supported claws:

| Claw | Runtime | How it gets into the container | Model selection |
|---|---|---|---|
| `openclaw` | Node.js CLI | bind-mount node + module + `~/.openclaw` | `--model` (per-agent) |
| `hermes` | Python venv | bind-mount standalone Python + venv | `--model` + `claw_configs/hermes/config.yaml` providers |
| `nanobot` | Python venv | bind-mount standalone Python + venv | `claw_configs/nanobot/config.json` |
| `zeroclaw` | single Rust binary | bind-mount binary | `claw_configs/zeroclaw/config.toml` |
| `generic` | Python repo ([lsdefine/GenericAgent](https://github.com/lsdefine/GenericAgent)) | bind-mount repo + venv | `--llm_no` index into `claw_configs/generic/mykey.py` |
| `dsh` | Python venv (`deepseek_harness`) | bind-mount standalone Python + venv + supplied runner | `--model` + rendered provider patch |

## Design

```
run_infer.py ──► orchestrator ──► SWEBenchWorkspace (Docker)  ── claw-agnostic
                     │                    │
                     └────────► BaseClawAdapter hooks ──────── claw-specific
                                (claw_swebench/claws/)
run_eval.py  ──► official SWE-bench harness (separate venv)
```

- **Claw-agnostic core** (`claw_swebench/`): dataset loading, container
  lifecycle, repo preparation, prompt rendering, runner-side patch
  collection/cleaning, predictions/state persistence, harness evaluation.
- **Claw adapters** (`claw_swebench/claws/`): each adapter implements
  `BaseClawAdapter` — extra `docker run` args (mounts), post-start
  provisioning, agent lifecycle, task launch, session backup, usage
  collection. Adding a claw = one new file + one registry entry.

Key fairness/contamination properties, enforced for every claw:

- **Same prompt.** All supplied adapters use the shared `prompts/default.txt`,
  containing the repository location, task constraints, and issue description.
- **No network answers.** The prompt forbids network use; OpenClaw
  additionally gets a 13-tool deny list (web/memory/session/cron tools);
  NanoBot's web tools are disabled in config; ZeroClaw's traffic goes
  through a tool-filtering proxy. Container egress is locked by default to
  the configured API domains (see *Configure the claw*).
- **Future-commit stripping.** The official Multilingual images retain the
  fix commit in git history (`git log --all` leaks the gold patch). Every
  workspace strips future tags/commits, expires reflogs, and GCs before the
  agent starts, then asserts zero future commits remain.
- **Runner-side patch collection.** The patch is always `git diff` taken by
  the runner after the agent exits — never agent-reported. Setup/lock-file
  and binary diffs are stripped (`patch.py`).
- **Per-instance isolation.** One fresh container per instance (pids/memory
  limited); OpenClaw additionally gets a throwaway agent per instance.

## Setup

### 1. Host requirements

- A Linux host with Docker, `iptables`, and a configured `sniproxy-swe`
  systemd service for the default network lock (details below).
- Docker with prebuilt SWE-bench instance images
  (`sweb.eval.x86_64.<instance_id>:latest`, or SWE-agent's
  `swebench/sweb.eval.x86_64.<id>` naming — both are auto-detected).
- Python 3.10+ with `pip install -r requirements.txt`.
- The official SWE-bench harness installed in its own venv for evaluation
  (default `/data/swe-bench-env`, override with `SWEBENCH_VENV`).

### 2. Install the claw(s) you want to run

Each claw's runtime lives on the host and is bind-mounted read-only into the
eval containers. Defaults (all overridable via env vars, see
`claw_swebench/config.py`):

| Env var | Default | Used by |
|---|---|---|
| `CLAW_PYTHON_HOME` | `/root/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu` | hermes, nanobot, generic, dsh |
| `OPENCLAW_NODE_BIN` / `OPENCLAW_MODULE_DIR` / `OPENCLAW_STATE_DIR` | `/usr/bin/node` / `/usr/lib/node_modules/openclaw` / `~/.openclaw` | openclaw |
| `HERMES_ENV_PATH` | `/opt/hermes-env` | hermes |
| `NANOBOT_ENV_PATH` | `/opt/nanobot-env` | nanobot |
| `ZEROCLAW_BIN` | `/usr/local/bin/zeroclaw` | zeroclaw |
| `GA_REPO_PATH` / `GA_ENV_PATH` | `/opt/genericagent` / `/opt/genericagent-env` | generic |
| `DSH_ENV_PATH` | `/opt/dsh-env` (must provide `deepseek_harness`) | dsh |

The standalone Python (`uv python install 3.12`) is required because the
SWE-bench images don't ship a usable Python 3.12; the venvs must be created
with that interpreter so they run inside any container.

### 3. Configure the claw

The tracked claw configs are templates. Set the host environment before
running Hermes, NanoBot, ZeroClaw, GenericAgent, DSH, or the Meta-Harness
proposer:

```bash
export OPENROUTER_API_KEY="your-api-key"
export OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
```

`OPENROUTER_BASE_URL` is mandatory for those adapters and must be an absolute
HTTP(S) URL including the API version path (for example, `/api/v1` for
OpenRouter). Missing or invalid values raise a `RuntimeError`; no default
API URL is inferred. Hermes and NanoBot receive the key and URL through rendered
configs; DSH renders the URL into its runner patches and forwards the key.
ZeroClaw renders its key and sends requests through the host tool-filter
proxy, which uses this URL as its upstream. GenericAgent validates the URL
and forwards both variables to its runtime, where `mykey.py` reads them.
Only `OPENROUTER_API_KEY` is in the shared key-forwarding list. OpenClaw uses
its own configured providers and credential store (`~/.openclaw`, or
`OPENCLAW_STATE_DIR`); this adapter does not consume these two variables.

Hermes, NanoBot, ZeroClaw, DSH, and the proposer substitute environment
placeholders into `.rendered/<claw>/` at runtime; these directories contain
credentials and are gitignored. GenericAgent reads its environment directly.
Edit the tracked templates for model/provider settings where needed; do not
put real credentials in them.

By default, `SWE_NETWORK_WHITELIST=openrouter.ai` enables the Linux egress
lock in `claw_swebench/netlock.py`. The runner creates the Docker bridge
`swe-locked` (`SWE_NETLOCK_NETWORK`) with subnet `172.30.0.0/16`
(`SWE_NETLOCK_SUBNET`), configures the `SWE-LOCK` iptables chain, and checks or
starts the `sniproxy-swe` service. Run with host privileges that permit Docker,
iptables, and systemd operations. Provision that service and its config at
`/etc/sniproxy-swe/sniproxy.conf` yourself: the code does not create a missing
proxy config or unit. Every comma-separated allowlisted domain must appear
in the config's `table swe_allow`; restart `sniproxy-swe` after changing it.
The containers use `--dns 127.0.0.1` and map allowlisted hosts to the bridge
gateway, routing API traffic through the SNI proxy.

For a custom API URL, add its hostname to both `SWE_NETWORK_WHITELIST` and
`table swe_allow`, and configure the proxy route to that endpoint. Include
any additional API hostnames required by OpenClaw's provider config. Setting
`SWE_NETWORK_WHITELIST=off` (or an empty value) disables all of this runner's
network control and leaves ordinary Docker networking; use that only when
another mechanism supplies the isolation needed for your evaluation.

## Run

Inference (patch generation):

```bash
python3 run_infer.py \
    --claw openclaw \
    --dataset multilingual \
    --run_id openclaw-multi-1 \
    --instance_file config/multilingual_300_instances.txt \
    --timeout 3600
```

`--timeout 3600` is the standard per-instance budget used in our
evaluations (also the built-in default — spelled out here so runs are
reproducible even if defaults change).

- `--claw {openclaw,hermes,nanobot,zeroclaw,generic,dsh}` — which harness.
- `--dataset {verified,multilingual}` — loads `config/<dataset>.yaml`.
- `--model`, `--timeout`, `--max_turns` — override per-claw defaults
  (`CLAW_DEFAULTS` in `config.py`). For nanobot/zeroclaw the model lives in
  the claw's own config file; `--model` is recorded as metadata.
- `--llm_no N` — generic only: selects the zero-based provider entry in
  `mykey.py` (default `1`).
- `--candidate PATH` — generic only: run a GenericAgent source tree, such as
  `agents/task_completion_guard`, instead of `GA_REPO_PATH`.
- `--reasoning_effort VALUE` — DSH only: forwarded to the supplied runner
  (default `xhigh`). DSH uses the supplied model-specific provider patches;
  its runner does not enforce `--max_turns`.
- `--instance_ids ID ...` / `--instance_file PATH` — select a subset; when
  both are supplied their IDs are combined. Evaluation accepts
  `--instance_ids ID ...` as well.
- `--workers N` — parallel instances (each in its own container).
- Re-running the same `--run_id` resumes (skips completed instances);
  `--no_resume` disables that.

Artifacts land in `artifacts/<run_id>/`: per-instance `prompt.txt`,
`agent_stdout.log` / `agent_stderr.log`, session logs, `git.patch`,
`metadata.json` (incl. token usage where available), plus shared
`predictions.jsonl` and `state.jsonl`.

Evaluation (official harness):

```bash
python3 run_eval.py --run_id openclaw-multi-1 --dataset multilingual
```

This reads `artifacts/<run_id>/predictions.jsonl` and resolves the dataset
from `config/<dataset>.yaml`. The explicit prediction path and full dataset
name remain available:

```bash
python3 run_eval.py \
    --predictions artifacts/openclaw-multi-1/predictions.jsonl \
    --dataset_name SWE-bench/SWE-bench_Multilingual \
    --run_id openclaw-multi-1
```

A paired Hermes run on the Verified-Mini subset:

```bash
python3 run_infer.py \
    --claw hermes --dataset verified --run_id hermes-vmini-1 \
    --instance_file config/verified_mini_50.txt --timeout 3600
python3 run_eval.py --run_id hermes-vmini-1 --dataset verified
```

Use a distinct `--run_id` per claw/run so harness logs don't collide.
Evaluation uses `--max_workers` for parallelism; inference uses `--workers`.

Optional Meta-Harness evolves GenericAgent source trees using a host Hermes
proposer, validation/smoke checks, benchmark inference/evaluation, and a
recorded frontier. It needs the installed GenericAgent tree (`GA_REPO_PATH`),
a host `hermes` CLI, the API environment above, and the benchmark runtime.
First build a seed and the provided `task_completion_guard` candidate:

```bash
python3 metaharness/seed.py
python3 metaharness/seed.py --candidate task_completion_guard
python3 run_infer.py \
    --claw generic --candidate agents/task_completion_guard \
    --dataset verified --run_id guard-vmini-1 \
    --instance_file config/verified_mini_50.txt
python3 run_eval.py --run_id guard-vmini-1 --dataset verified
```

`seed.py` copies the installed tree to `agents/baseline_generic` and applies
`metaharness/task_completion_guard.diff` to create the candidate. Use
`--force` to rebuild an existing seed/candidate. Generated `agents/` trees
are gitignored. The outer loop runs real model calls and evaluations:

```bash
python3 metaharness/meta_harness.py \
    --run-name generic-evolve-1 --iterations 5 --task-set full350
```

The current loop fixes its model/provider index and forces
`SWE_NETWORK_WHITELIST=openrouter.ai` for benchmark subprocesses; a custom API
host requires adjusting that loop configuration as well as the SNI allow
table. Its only task set is `full350` (the two supplied instance lists).
Outputs go to `jobs/`, `logs/`, `artifacts/`, and `agents/`; see
`python3 metaharness/meta_harness.py --help` for concurrency, baseline reuse,
and proposer limits.
The `--run-name` namespaces benchmark and smoke artifact IDs as well as jobs
and logs. Reuse the same name to resume benchmark instances, including on a
later date; choose a new name for a separate experiment. Each smoke check
uses fresh artifacts.

## Adding a new claw

1. Create `claw_swebench/claws/<name>.py` implementing `BaseClawAdapter`:
   - `container_run_args(instance_id)` — bind mounts for your runtime;
   - `send_task(...)` — launch the agent in the container, return `AgentResult`;
   - optionally `post_container_start`, `create_agent`/`delete_agent`,
     `backup_session`, `collect_usage`, `prompt_template`.
2. Register the class in `claw_swebench/claws/__init__.py` and add defaults
   to `CLAW_DEFAULTS` in `config.py`.
3. Keep the default prompt. Only add tool-name guidance if your claw's tools
   genuinely need it, and keep the rest of the template byte-identical.

## Notes

- **Resource limits**: every container runs with `--pids-limit 300
  --memory 8g` (override via `CLAW_PIDS_LIMIT` / `CLAW_CONTAINER_MEMORY`).
- **Instance lists**: `config/multilingual_300_instances.txt` and
  `config/verified_mini_50.txt` together form the 350-instance full set. The
  80-instance Lite subset is selected by the cost-aware, rank-aware procedure
  described in the paper.
