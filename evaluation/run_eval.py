#!/usr/bin/env python3
"""
run_eval.py
Evaluate UI-TARS (Model A or B) on the 15 held-out WebArena shopping tasks.

Usage
─────
# Model A — base UI-TARS, no fine-tuning
python run_eval.py --model_name model_a \
    --model_path /data/gaze-web-nav-training/models/UI-TARS-1.5-7B

# Model B — BC fine-tuned
python run_eval.py --model_name model_b \
    --model_path /data/gaze-web-nav-training/models/UI-TARS-1.5-7B \
    --lora_path  /data/gaze-web-nav-training/outputs/bc_baseline

Options
───────
--task_ids 48 49 146   run only specific tasks (default: all 15)
--max_steps N          max actions per task (default: 15)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Add webarena-tasks to Python path ────────────────────────────────────────
sys.path.insert(0, "/data/webarena-setup/webarena-tasks")

from browser_env import ScriptBrowserEnv
from browser_env.actions import (
    ActionParsingError,
    create_playwright_action,
    create_stop_action,
    create_none_action,
)
from evaluation_harness import evaluator_router

from uitars_agent import UITARSAgent

# ── Paths ─────────────────────────────────────────────────────────────────────
TASK_CONFIG_DIR = "/home/liralab-widowx/gaze-web-nav/evaluation/task_configs"
RESULTS_DIR     = "/home/liralab-widowx/gaze-web-nav/evaluation/results"

# ── Viewport — must match training screenshot size ────────────────────────────
VIEWPORT = {"width": 1920, "height": 878}


# ─────────────────────────────────────────────────────────────────────────────

def run_task(agent: UITARSAgent, config_file: str, max_steps: int) -> dict:
    """Run a single WebArena task. Returns a result dict."""
    with open(config_file) as f:
        config = json.load(f)

    task_id    = config["task_id"]
    intent     = config["intent"]
    eval_types = config["eval"]["eval_types"]
    needs_ans  = "string_match" in eval_types

    print(f"\n{'─'*64}")
    print(f"Task {task_id} | {eval_types}")
    print(f"Intent: {intent[:80]}")

    env = ScriptBrowserEnv(
        headless=True,
        slow_mo=100,
        observation_type="image",
        current_viewport_only=True,
        viewport_size=VIEWPORT,
    )

    result = {
        "task_id"   : task_id,
        "intent"    : intent,
        "eval_types": eval_types,
        "score"     : 0.0,
        "steps"     : 0,
        "actions"   : [],
        "error"     : None,
    }

    try:
        obs, info  = env.reset(options={"config_file": config_file})
        trajectory = [{"observation": obs, "info": info}]
        done       = False

        for step in range(max_steps):
            screenshot = obs["screenshot"]

            # ── Model inference ───────────────────────────────────────────
            raw = agent.predict(screenshot, intent, needs_answer=needs_ans)
            print(f"  [{step+1:2d}] {raw[:90]}")
            result["actions"].append(raw)

            # ── Convert to playwright ─────────────────────────────────────
            pw_str = agent.to_playwright(raw)

            # ── Parse action ──────────────────────────────────────────────
            try:
                if "page.stop(" in pw_str:
                    action = create_stop_action(pw_str)
                    done   = True
                else:
                    action = create_playwright_action(pw_str)
            except (ActionParsingError, Exception) as e:
                print(f"  [WARN] Parse error: {e}")
                action = create_none_action()

            action["raw_prediction"] = raw
            trajectory.append(action)

            if done:
                print(f"  → Agent stopped after {step+1} step(s)")
                break

            # ── Execute ───────────────────────────────────────────────────
            obs, _, terminated, _, info = env.step(action)
            trajectory.append({"observation": obs, "info": info})
            result["steps"] = step + 1

            if terminated:
                break

        # ── Evaluate ──────────────────────────────────────────────────────
        try:
            evaluator = evaluator_router(config)
            score     = evaluator(trajectory, intent, env.page, env.client)
            result["score"] = float(score)
        except Exception as e:
            result["error"] = f"Eval error: {e}"
            print(f"  [ERROR] {e}")

    except Exception as e:
        result["error"] = str(e)
        print(f"  [FATAL] {e}")

    finally:
        env.close()

    status = "✅" if result["score"] > 0 else "❌"
    print(f"  {status} Score: {result['score']}")
    return result


# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path", required=True,
                        help="Path to base UI-TARS model")
    parser.add_argument("--lora_path",  default=None,
                        help="Path to LoRA adapter dir (omit for Model A)")
    parser.add_argument("--model_name", default=None,
                        help="Label for results file (default: model_a / model_b)")
    parser.add_argument("--task_ids",   nargs="+", type=int, default=None,
                        help="Run specific task IDs only")
    parser.add_argument("--max_steps",  type=int, default=15,
                        help="Max actions per task")
    args = parser.parse_args()

    model_name = args.model_name or ("model_b" if args.lora_path else "model_a")

    # ── Load agent ────────────────────────────────────────────────────────
    agent = UITARSAgent(args.model_path, args.lora_path)

    # ── Collect task configs ──────────────────────────────────────────────
    configs = sorted(Path(TASK_CONFIG_DIR).glob("*.json"))
    if args.task_ids:
        ids     = set(args.task_ids)
        configs = [c for c in configs if int(c.stem) in ids]

    print(f"\nEvaluating [{model_name}] on {len(configs)} tasks "
          f"(max {args.max_steps} steps each)\n")

    # ── Run tasks ─────────────────────────────────────────────────────────
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, f"{model_name}_results.json")
    results  = []

    for cfg in configs:
        r = run_task(agent, str(cfg), args.max_steps)
        results.append(r)
        # Save after each task so partial results are never lost
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)

    # ── Summary ───────────────────────────────────────────────────────────
    n       = len(results)
    success = sum(r["score"] for r in results)
    sr      = success / n if n else 0.0

    print(f"\n{'='*64}")
    print(f"Model : {model_name}")
    print(f"Tasks : {n}")
    print(f"Score : {success:.0f} / {n}  ({sr:.1%})")
    print()
    for r in results:
        flag = "✅" if r["score"] > 0 else "❌"
        print(f"  {flag} [{r['task_id']:4d}] {r['score']:.2f}  {r['intent'][:55]}")

    print(f"\nResults → {out_path}")


if __name__ == "__main__":
    main()
