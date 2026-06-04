#!/usr/bin/env python3
"""
preprocess.py — Convert WebArena recordings to LLaMA-Factory training format.

Reads:  /data/gaze-web-nav-training/recordings/task*_traj*/
Writes: /home/liralab-widowx/gaze-web-nav/training/dataset/
          webarena_bc.json       — BC dataset (screenshot + action), no gaze
          webarena_bc_gaze.json  — Same + nearest gaze point per step
          dataset_info.json      — LLaMA-Factory dataset registry
"""

import json
import os
import re
import glob
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
RECORDINGS_DIR  = "/data/gaze-web-nav-training/recordings"
TASKS_FILE      = "/home/liralab-widowx/gaze-web-nav/data_collection/sampled_shopping_tasks.json"
OUTPUT_DIR      = "/home/liralab-widowx/gaze-web-nav/training/dataset"

# ── Screenshot dimensions ────────────────────────────────────────────────────
IMG_W = 1920
IMG_H = 878

# ── Screen dimensions (for gaze coordinate mapping) ─────────────────────────
# GazePoint tracks the full screen; the browser content area is offset from
# the top by the browser chrome (tabs + address bar ≈ 100px).
SCREEN_W         = 1920
SCREEN_H         = 1080
BROWSER_OFFSET_Y = 100   # pixels from top of screen to browser content area

# ── Gaze window ──────────────────────────────────────────────────────────────
GAZE_WINDOW_SEC = 0.2    # use gaze points in the 200ms before each action

# ── Scroll step size ─────────────────────────────────────────────────────────
SCROLL_UNIT_PX = 400     # ~half viewport height = 1 scroll step


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────────

def norm_coord(x_px, y_px):
    """Convert screenshot pixel coordinates → UI-TARS 0-1000 range."""
    xn = round(max(0, min(1000, x_px / IMG_W * 1000)))
    yn = round(max(0, min(1000, y_px / IMG_H * 1000)))
    return xn, yn


def screen_to_screenshot(fpogx, fpogy):
    """
    Convert GazePoint normalised screen coords (0-1) to screenshot pixel coords.
    GazePoint coords are relative to the full screen; the screenshot starts
    BROWSER_OFFSET_Y pixels below the top of the screen.
    """
    sx = fpogx * SCREEN_W
    sy = fpogy * SCREEN_H - BROWSER_OFFSET_Y
    sx = max(0.0, min(float(IMG_W - 1), sx))
    sy = max(0.0, min(float(IMG_H - 1), sy))
    return sx, sy


# ─────────────────────────────────────────────────────────────────────────────
# Action conversion
# ─────────────────────────────────────────────────────────────────────────────

def action_to_uitars(action, prev_scroll_y):
    """
    Convert one actions.json entry to a UI-TARS action string.
    Returns (action_string, new_prev_scroll_y).
    Returns (None, prev_scroll_y) for skipped actions.
    """
    atype = action["type"]

    if atype in ("start", "navigate"):
        # Not model actions — skip. Reset scroll baseline on navigate.
        new_scroll = 0 if atype == "navigate" else prev_scroll_y
        return None, new_scroll

    if atype == "click":
        xn, yn = norm_coord(action["x"], action["y"])
        return f"click(start_box='({xn},{yn})')", prev_scroll_y

    if atype == "type":
        value = action.get("value", "").replace("'", "\\'")
        return f"type(content='{value}')", prev_scroll_y

    if atype == "scroll":
        scroll_y = action["scrollY"]
        delta    = scroll_y - prev_scroll_y
        direction  = "down" if delta >= 0 else "up"
        step_count = max(1, round(abs(delta) / SCROLL_UNIT_PX))
        return (
            f"scroll(start_box='(500,500)', direction='{direction}', step_count={step_count})",
            scroll_y
        )

    if atype == "select":
        # No coordinates recorded for SELECT — represent as type action
        # using the human-readable option label.
        label = action.get("label", action.get("value", ""))
        label = label.replace("'", "\\'")
        return f"type(content='{label}')", prev_scroll_y

    # Unknown type — skip
    return None, prev_scroll_y


# ─────────────────────────────────────────────────────────────────────────────
# Gaze helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_gaze_raw(raw_xml):
    """
    Parse one GazePoint XML record.
    Returns (fpogx, fpogy) if valid, else None.
    """
    try:
        v = re.search(r'FPOGV="([^"]+)"', raw_xml)
        if v and int(v.group(1)) == 0:
            return None                         # blink / lost tracking
        x = re.search(r'FPOGX="([^"]+)"', raw_xml)
        y = re.search(r'FPOGY="([^"]+)"', raw_xml)
        if not x or not y:
            return None
        return float(x.group(1)), float(y.group(1))
    except Exception:
        return None


def compute_gaze_point(gaze_data, action_timestamp):
    """
    Average all valid gaze points in the 200ms window before action_timestamp.
    Falls back to single nearest valid point if window is empty.
    Returns a dict with pixel and normalised coordinates, or None.
    """
    if not gaze_data:
        return None

    t_end   = action_timestamp
    t_start = action_timestamp - GAZE_WINDOW_SEC

    # Collect valid points in the window
    window_pts = []
    for g in gaze_data:
        if t_start <= g["t"] <= t_end:
            parsed = parse_gaze_raw(g["raw"])
            if parsed:
                window_pts.append((g["t"], parsed[0], parsed[1]))

    # Fall back: nearest valid point anywhere in the stream
    if not window_pts:
        candidates = []
        for g in gaze_data:
            parsed = parse_gaze_raw(g["raw"])
            if parsed:
                candidates.append((abs(g["t"] - action_timestamp), g["t"], parsed[0], parsed[1]))
        if not candidates:
            return None
        candidates.sort()
        _, gt, fpogx, fpogy = candidates[0]
        window_pts = [(gt, fpogx, fpogy)]

    # Average
    avg_fpogx = sum(p[1] for p in window_pts) / len(window_pts)
    avg_fpogy = sum(p[2] for p in window_pts) / len(window_pts)

    px, py   = screen_to_screenshot(avg_fpogx, avg_fpogy)
    xn, yn   = norm_coord(px, py)

    return {
        "n_points" : len(window_pts),
        "fpogx"    : round(avg_fpogx, 4),
        "fpogy"    : round(avg_fpogy, 4),
        "pixel_x"  : round(px),
        "pixel_y"  : round(py),
        "norm_x"   : xn,
        "norm_y"   : yn,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load task intents
    with open(TASKS_FILE) as f:
        raw = json.load(f)
    task_intents = {str(t["task_id"]): t["intent"] for t in raw}
    print(f"Loaded {len(task_intents)} task intents")

    # Find recording folders
    rec_dirs = sorted(glob.glob(os.path.join(RECORDINGS_DIR, "task*_traj*")))
    rec_dirs = [d for d in rec_dirs if os.path.isdir(d)]
    print(f"Found {len(rec_dirs)} recording folders\n")

    # Split: traj1 + traj2 → train, traj3 → val
    # Splitting at trajectory level avoids leakage (same task's screenshots
    # never appear in both train and val).
    bc_train   = [];  bc_val   = []
    gz_train   = [];  gz_val   = []
    skipped    = 0

    for rec_dir in rec_dirs:
        name = os.path.basename(rec_dir)

        # Load session info
        si_path = os.path.join(rec_dir, "session_info.json")
        if not os.path.exists(si_path):
            print(f"  SKIP {name} — no session_info.json")
            continue
        with open(si_path) as f:
            session_info = json.load(f)

        task_id    = str(session_info["task_id"])
        trajectory = str(session_info.get("trajectory", "1"))
        is_val     = (trajectory == "3")   # traj3 → val, traj1+traj2 → train
        intent     = task_intents.get(task_id)
        if intent is None:
            print(f"  SKIP {name} — task_id {task_id} not in sampled list")
            continue

        # Load actions
        actions_path = os.path.join(rec_dir, "actions.json")
        if not os.path.exists(actions_path):
            print(f"  SKIP {name} — no actions.json")
            continue
        with open(actions_path) as f:
            actions = json.load(f)

        # Load gaze
        gaze_path = os.path.join(rec_dir, "gaze.json")
        gaze_data = []
        if os.path.exists(gaze_path):
            with open(gaze_path) as f:
                gaze_data = json.load(f)

        screenshots_dir = os.path.join(rec_dir, "screenshots")
        prev_scroll_y   = 0
        step_count      = 0

        for action in actions:
            uitars_str, prev_scroll_y = action_to_uitars(action, prev_scroll_y)

            if uitars_str is None:
                skipped += 1
                continue

            # Screenshot
            screenshot_file = action.get("screenshot")
            if not screenshot_file:
                skipped += 1
                continue
            screenshot_path = os.path.join(screenshots_dir, screenshot_file)
            if not os.path.exists(screenshot_path):
                skipped += 1
                continue

            # Build training example
            example = {
                "conversations": [
                    {
                        "from" : "human",
                        "value": (
                            f"<image>\n"
                            f"You are a web browser agent. Complete the following task.\n"
                            f"Task: {intent}\n"
                            f"What is the next action?"
                        )
                    },
                    {
                        "from" : "gpt",
                        "value": f"Action: {uitars_str}"
                    }
                ],
                "images": [screenshot_path]
            }

            bc_dataset  = bc_val   if is_val else bc_train
            gz_dataset  = gz_val   if is_val else gz_train
            bc_dataset.append(example)

            # Gaze example (same + metadata)
            gaze_pt = compute_gaze_point(gaze_data, action["timestamp"])
            gaze_example = dict(example)
            gaze_example["gaze"] = gaze_pt
            gaze_example["meta"] = {
                "recording"  : name,
                "task_id"    : task_id,
                "trajectory" : trajectory,
                "split"      : "val" if is_val else "train",
                "step"       : action["step"],
                "action_type": action["type"],
            }
            gz_dataset.append(gaze_example)
            step_count += 1

        split_label = "val" if is_val else "train"
        print(f"  {name} [{split_label}]: {step_count} steps  ({len(gaze_data)} gaze pts)")

    # ── Save outputs ─────────────────────────────────────────────────────────
    def save(data, filename):
        path = os.path.join(OUTPUT_DIR, filename)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return path

    save(bc_train,  "webarena_bc_train.json")
    save(bc_val,    "webarena_bc_val.json")
    save(gz_train,  "webarena_bc_gaze_train.json")
    save(gz_val,    "webarena_bc_gaze_val.json")
    # Full dataset (train+val combined) — kept for reference
    save(bc_train + bc_val, "webarena_bc.json")
    save(gz_train + gz_val, "webarena_bc_gaze.json")

    dataset_info = {
        "webarena_bc_train": {
            "file_name" : "webarena_bc_train.json",
            "formatting": "sharegpt",
            "columns"   : {"messages": "conversations", "images": "images"}
        },
        "webarena_bc_val": {
            "file_name" : "webarena_bc_val.json",
            "formatting": "sharegpt",
            "columns"   : {"messages": "conversations", "images": "images"}
        },
        "webarena_bc": {
            "file_name" : "webarena_bc.json",
            "formatting": "sharegpt",
            "columns"   : {"messages": "conversations", "images": "images"}
        },
        "webarena_bc_gaze_train": {
            "file_name" : "webarena_bc_gaze_train.json",
            "formatting": "sharegpt",
            "columns"   : {"messages": "conversations", "images": "images"}
        },
        "webarena_bc_gaze_val": {
            "file_name" : "webarena_bc_gaze_val.json",
            "formatting": "sharegpt",
            "columns"   : {"messages": "conversations", "images": "images"}
        },
    }
    di_path = os.path.join(OUTPUT_DIR, "dataset_info.json")
    with open(di_path, "w") as f:
        json.dump(dataset_info, f, indent=2)

    # ── Summary ──────────────────────────────────────────────────────────────
    gz_all = gz_train + gz_val
    gaze_covered = sum(1 for g in gz_all if g["gaze"] is not None)
    print(f"\n{'─'*50}")
    print(f"Train examples     : {len(bc_train)}  (traj1 + traj2)")
    print(f"Val   examples     : {len(bc_val)}   (traj3)")
    print(f"Total examples     : {len(bc_train) + len(bc_val)}")
    print(f"Skipped steps      : {skipped}")
    print(f"Gaze coverage      : {gaze_covered}/{len(gz_all)} steps have gaze")
    print(f"\nOutputs saved to   : {OUTPUT_DIR}/")
    print(f"  webarena_bc_train.json      ({len(bc_train)} examples)")
    print(f"  webarena_bc_val.json        ({len(bc_val)} examples)")
    print(f"  webarena_bc.json            ({len(bc_train)+len(bc_val)} combined)")
    print(f"  dataset_info.json")


if __name__ == "__main__":
    main()
