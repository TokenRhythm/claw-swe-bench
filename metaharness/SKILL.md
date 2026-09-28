---
name: meta-harness-swebench-genericagent
description: Run one iteration of GenericAgent scaffold evolution for SWE-bench.
---

# Meta-Harness (SWE-bench, GenericAgent track)

Run ONE iteration of agent scaffold evolution.

**You do NOT run benchmarks.** You analyze results + failed trajectories, propose an agent variant, and implement it. The outer loop (`metaharness/meta_harness.py`) handles validation, smoke test and benchmarking.

## CRITICAL CONSTRAINTS

- You MUST produce 1 new agent variant every iteration
- Do NOT write "the frontier is optimal" or "stop iterating", or abort early.

### Anti-overfitting rules

- **No task-specific hints.** Do not hardcode knowledge about specific benchmark instances or repositories. Agents must be general-purpose.
- **Never mention instance ids, repository names, issue numbers, file paths or test names of benchmark instances** in agent code, prompts, or comments. No references like "if the repo is django" or "for the rubocop tasks". If your improvement only helps one instance or one repository, it's too specific.
- **General guidance is OK.** Rules like "locate and run the project's existing tests for the module you touched before finishing" are fine -- they happen to help specific instances but apply broadly. The test: would this advice be useful to a human developer working on MANY unfamiliar repositories in MANY languages?
- **If in doubt, make it more general.** "Run the repository's own test command for the affected module" > "run `bundle exec rspec spec/foo_spec.rb`".

### Anti-cheat rules (hard; the validator rejects violations)

- Never add code or prompt text that inspects git history beyond the checked-out commit (`git log --all`, `git show <sha>`, `git reflog`, `git branch -a`, reading `.git/` internals) or that reaches the network (`curl`, `wget`, `pip install`, `git fetch/clone/pull`). The evaluation container's egress is locked to the LLM endpoint; such calls only waste turns.
- Do not enable or extend the web tools (`web_scan`, `web_execute_js`) or `ask_user` for this task -- there is no browser and no user.
- Do not modify `claw_configs/generic/` (LLM credentials / model selection), `claw_swebench/` (the eval framework), `config/`, `prompts/`, `run_infer.py`, `run_eval.py`. The model (`deepseek/deepseek-v4.1-flash`, reasoning_effort xhigh) is frozen.

## CONTEXT

You are evolving the **GenericAgent** scaffold for SWE-bench. The unmodified baseline is the directory `agents/baseline_generic/` -- a full copy of the GenericAgent source tree.

**The search space is arbitrary Python code.** You can rewrite the agent loop, change how the LLM is called, add or remove tools, change tool descriptions, rewrite the system prompt, transform the incoming task text, intercept and reshape tool outputs, change context/working-memory management, add pre-run environment probing, add a finish-time verification step -- anything that's expressible in Python is fair game. The only constraints are that the tree stays runnable through the same task-mode entry point (below) and the anti-cheat rules above.

**How the agent is run inside the SWE-bench container:** the eval adapter bind-mounts `agents/<name>/` read-only at `/opt/genericagent` (only `temp/` and `memory/` are writable) and executes

```
python /opt/genericagent/agentmain.py --task <id> --llm_no 1 --nobg --verbose
```

with `cwd=/testbed` (the repository to fix). GenericAgent reads the task from `temp/<id>/input.txt`, runs the loop, and must write `temp/<id>/output.txt` ending with the literal sentinel `[ROUND END]`. The adapter then collects `git diff` of `/testbed` as the patch. `--llm_no 1` selects the frozen model config from `/opt/generic-config/mykey.py` (mounted separately; not yours to change). The task prompt in `input.txt` is built by the frozen adapter -- if you want a different task framing, transform it inside `agentmain.py`.

**Model: DeepSeek-V4.1-Flash (OpenRouter, reasoning_effort xhigh).** Evaluation uses the SWE-bench instance split named in the task prompt (Verified + Multilingual, many languages: Python, Java, Go, Rust, JS/TS, Ruby, PHP, C/C++), 1 trial per instance, 3600 s wall clock per instance. Reward = the official grader's `resolved` (FAIL_TO_PASS tests pass and PASS_TO_PASS tests still pass).

**Key files to read (inside the agent tree):**

- `agent_loop.py` -- `agent_runner_loop()`: the ReAct loop (LLM call -> tool dispatch -> next prompt). `BaseHandler.dispatch()` routes tool `X` to `handler.do_X()`.
- `ga.py` -- `GenericAgentHandler`: the tools (`do_code_run`, `do_file_read`, `do_file_patch`, `do_file_write`, `do_no_tool`, `do_update_working_checkpoint`, ...), `turn_end_callback()` (what is appended after every turn), `_get_anchor_prompt()` / `_fold_earlier()` (working-memory / history folding), `code_run()` (how scripts execute; note `code_type` python vs bash), `file_read()` (chunking, line numbers).
- `agentmain.py` -- `get_system_prompt()`, `GenericAgent.run()` task mode (`max_turns`, how `input.txt` becomes the first user message, how `output.txt` and `[ROUND END]` are written).
- `assets/sys_prompt_en.txt` -- the system prompt. `assets/tools_schema.json` -- the tool schema sent to the model (names, descriptions, parameters).
- `llmcore.py` -- `NativeOAISession` / `ToolClient.chat()`: how messages, tools and `reasoning_effort` are sent; `_record_usage()` prints the `[Cache] input=... cached=...` / `[Output] tokens=...` lines the adapter parses -- keep those lines intact.
- `memory/` -- GenericAgent's global memory files that get appended to the system prompt via `get_global_memory()`.

You should copy one of the agents in `agents/` as a starting point. **You can rewrite ANY file in your copy.** Never import from other candidate trees. Copy any code you want to reuse.

## CANDIDATE DESIGN

Each candidate is a **directory** `agents/<name>/` containing the full agent tree (copy the parent with `cp -r agents/baseline_generic agents/<name>`; never edit `baseline_generic/` or any previous candidate).

The tree must keep: `agentmain.py` (task-mode CLI `--task/--llm_no/--nobg`, writes `[ROUND END]`), `agent_loop.py`, `ga.py`, `llmcore.py`, `assets/tools_schema.json`, `assets/sys_prompt_en.txt`, `memory/`. The validator compiles every `.py`, parses `tools_schema.json`, checks the task-mode contract, and rejects new forbidden patterns (see Anti-cheat).

### What you can and cannot modify

- **CAN**: edit anything inside your new `agents/<name>/` directory freely.
- **CANNOT**: modify any existing agent directory, `metaharness/`, `claw_swebench/`, `claw_configs/`, `config/`, `prompts/`, `run_infer.py`, `run_eval.py`. (The outer loop hashes these; a change discards the iteration.)

### Design principles

- Your primary goal is to improve the agent's resolve rate on the eval split.
- One mechanism per candidate. Each candidate tests exactly one hypothesis. If you're tempted to add "and also..." -- that's the next iteration.
- Mechanism-first. Identify a specific failure mode or hypothesis from trajectories, then design changes that target it. Never add changes speculatively.

## WORKFLOW

### Step 1: Analyze

1. Read state files (paths given in the task prompt):
   - `frontier_val.json` -- current best agent per instance and overall
   - `evolution_summary.jsonl` -- what's been tried, what worked/didn't, plus `rollout_metrics` (cost, turns, timeouts, empty patches per instance)
2. **Deep-read failed AND successful trajectories.** Most important step.
   For every job the task prompt lists `artifacts/<run_id>/<instance_id>/`:
   - `agent_stdout.log` -- the full verbose trace: every LLM turn, `🛠️ Tool:` call with args, tool output, `[Cache]`/`[Output]` token lines, `### [WORKING MEMORY]` blocks. This is where you see what actually happened.
   - `input.txt` (the task the agent received), `output.txt` (its final answer), `git.patch` (what it submitted; empty = gave up or produced nothing), `metadata.json` (state, duration, finish_reason, token usage).
   And the grader's `report.json` (`resolved`, which FAIL_TO_PASS / PASS_TO_PASS tests failed) plus `test_output.txt` under the grading directory.
   Look for recurring mechanics: wrong `code_type` (bash written into a python script), never running tests, editing tests instead of source, re-reading huge files, giving up early, `MAX_TURNS_EXCEEDED`, 3600 s timeouts, patch applies but wrong behaviour, broken/partial `file_patch` edits, working-memory folding losing the plan.
3. Read agent implementations in `agents/*/` (at least `agent_loop.py`, `ga.py`, `agentmain.py`, `assets/sys_prompt_en.txt`, `assets/tools_schema.json`).
4. Write down:

```
STATE: <5-line summary: current scores, what's been tried, avg tokens/turns/cost/timeouts>

HYPOTHESIS: "<falsifiable claim about what will improve scores>"
CANDIDATE: name=<snake_case>, changes="<specific changes, per file>", prediction="<expected resolve-rate improvement AND expected token/turn impact>"
```

### Step 2: Implement

1. Copy the parent: `cp -r agents/baseline_generic agents/<snake_case_name>` (or copy the frontier agent).
2. Make targeted changes according to Step 1.
3. **Smoke test**: `python3 metaharness/meta_harness.py --validate-agent <snake_case_name>` must print `OK`.
4. Note the file paths you changed.

### Step 3: Write pending_eval.json

Write `pending_eval.json` to the path specified in the task prompt:

```json
{
  "iteration": <N>,
  "candidates": [
    {
      "name": "<name>",
      "agent_dir": "agents/<name>",
      "parent": "baseline_generic or <previous candidate>",
      "hypothesis": "<falsifiable claim>",
      "changes": "<what was changed, per file>",
      "expected_efficiency": "<expected token/turn impact>"
    }
  ]
}
```

Output: `CANDIDATES: <name1>`

## IMPORTANT NOTES

- **Always keep the directory name == candidate name**; the outer loop mounts `agents/<name>` by that name.
- `agent_runner_loop()` is a generator; tool handlers `do_*` are generators too (they `yield` progress text and `return StepOutcome(...)`). Keep that protocol if you add tools: `StepOutcome(data, next_prompt, should_exit)` -- `next_prompt=None` ends the task, `should_exit=True` aborts.
- Adding a tool = add `do_<name>(self, args, response)` in `ga.py` **and** an entry in `assets/tools_schema.json` (OpenAI function-calling format). Removing a tool = delete both.
- The adapter parses token usage from stdout lines `[Cache] input=N cached=M` and `[Output] tokens=N` printed by `llmcore._record_usage()`; do not change their format.
- `GA_LANG=en` is set by the adapter, so the English prompt/schema files (`*_en.txt`, `tools_schema.json`) are the ones in use.
- Everything under the mounted tree except `temp/` and `memory/` is read-only at runtime; write scratch files under `temp/`.
- The whole run for one instance must finish within 3600 s and `max_turns` (300 in the baseline); trajectories that hit either limit are graded on whatever `git diff` exists at that moment.
