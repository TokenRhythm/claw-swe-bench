#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deepseek_harness import DeepSeekHarness

DEFAULT_PERSONA = "You are an expert software engineer working in a code repository."


def _aggregate_usage(events: list[dict]) -> dict:
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    total_tokens = 0
    calls = 0
    turns = set()
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "assistant/message":
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        if "turn" in data:
            turns.add(data.get("turn"))
        usage = data.get("usage")
        if isinstance(usage, dict):
            calls += 1
            input_tokens += int(usage.get("inputTokens", 0) or 0)
            output_tokens += int(usage.get("outputTokens", 0) or 0)
            cache_read_tokens += int(usage.get("cacheReadTokens", 0) or 0)
            total_tokens += int(usage.get("totalTokens", 0) or 0)
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read_tokens,
        "totalTokens": total_tokens,
        "calls": calls,
        "turns": len(turns),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--model", default="z-ai/glm-5.1")
    p.add_argument("--provider", default="openrouter")
    p.add_argument("--session-id", required=True)
    p.add_argument("--dsh-home", required=True)
    p.add_argument("--patch", required=True)
    p.add_argument("--cwd", default="/testbed")
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--persona", default=DEFAULT_PERSONA)
    p.add_argument("--reasoning-effort", default=None,
                   help="Passed straight to DeepSeekHarness(reasoning_effort=...), e.g. xhigh. Omit = provider default.")
    args = p.parse_args()

    prompt = Path(args.prompt_file).read_text()

    dsh_home = Path(args.dsh_home)
    dsh_home.mkdir(parents=True, exist_ok=True)

    import os
    os.environ.setdefault("DSH_SYSTEM_PROMPT", args.persona)

    try:
        with DeepSeekHarness(
            provider=args.provider,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_tokens=args.max_tokens,
            cwd=args.cwd,
            dsh_home=str(dsh_home),
            profile="sdk-minimal",
            patches=(args.patch,),
        ) as harness:
            result = harness.run(prompt, session_id=args.session_id)
        usage = _aggregate_usage(result.events)
        out = {
            "status": "ok",
            "final_response": result.final_response,
            "finish_reason": result.finish_reason,
            "session_id": result.session_id,
            "usage": usage,
            "n_events": len(result.events),
        }
    except Exception as e:
        out = {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }

    print("DSH_RESULT_JSON " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
