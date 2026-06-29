#!/usr/bin/env python3
"""
Collection progress tracker for the gaze-web-nav demos.

Scans recordings/ against collection_checklist.csv and reports which train/val
tasks are complete (all --demos trajectories present), in progress, or not
started -- plus the next tasks to record.

A trajectory folder task{id}_traj{n} COUNTS only if it has a non-empty
actions.json AND a non-empty gaze.json, so empty/aborted runs and any gaze-off
runs are never miscounted as done.

Usage:
  python progress.py
  python progress.py --next 12
  python progress.py --recordings ./recordings --checklist collection_checklist.csv
"""
import argparse
import csv
import json
from pathlib import Path


def traj_ok(folder, require_gaze=True):
    """A trajectory counts if actions.json is a non-empty list (and gaze too, unless disabled)."""
    a = folder / "actions.json"
    if not a.exists():
        return False
    try:
        acts = json.loads(a.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not (isinstance(acts, list) and len(acts) > 0):
        return False
    if require_gaze:
        g = folder / "gaze.json"
        if not g.exists():
            return False
        try:
            gaze = json.loads(g.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not (isinstance(gaze, list) and len(gaze) > 0):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recordings", default="./recordings")
    ap.add_argument("--checklist", default="collection_checklist.csv")
    ap.add_argument("--demos", type=int, default=3)
    ap.add_argument("--next", type=int, default=8)
    ap.add_argument("--no-gaze-check", action="store_true",
                    help="count a trajectory even if gaze.json is empty")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.checklist, encoding="utf-8")))
    by_id = {int(r["task_id"]): r for r in rows}
    rec = Path(args.recordings)
    require_gaze = not args.no_gaze_check

    done, partial, todo, total_traj = [], [], [], 0
    for tid, r in by_id.items():
        have = sum(1 for n in range(1, args.demos + 1)
                   if (rec / f"task{tid}_traj{n}").is_dir()
                   and traj_ok(rec / f"task{tid}_traj{n}", require_gaze))
        total_traj += have
        if have >= args.demos:
            done.append(tid)
        elif have > 0:
            partial.append((tid, have))
        else:
            todo.append(tid)

    n_tasks = len(rows)
    target = n_tasks * args.demos
    print(f"Tasks : {len(done)}/{n_tasks} complete  "
          f"({len(partial)} in progress, {len(todo)} not started)")
    bar = int(40 * total_traj / target) if target else 0
    print(f"Demos : {total_traj}/{target}  [{'#'*bar}{'.'*(40-bar)}]  "
          f"{(100*total_traj/target) if target else 0:.1f}%")

    if partial:
        print("\nFinish these (already started):")
        for tid, have in sorted(partial):
            print(f"  task {tid:<5} {have}/{args.demos}  [{by_id[tid]['split']}]  "
                  f"{by_id[tid]['intent'][:62]}")

    queue = [tid for tid, _ in sorted(partial)] + sorted(todo)
    if queue:
        print(f"\nNext {min(args.next, len(queue))} to record:")
        for tid in queue[:args.next]:
            print(f"  task {tid:<5} [{by_id[tid]['split']}]  {by_id[tid]['intent'][:62]}")
    else:
        print("\n*** Collection complete - every train/val task has all demos. ***")

    # flag stray folders not in the checklist (e.g. leftover task9999 test runs)
    if rec.is_dir():
        valid = {f"task{t}_traj{n}" for t in by_id for n in range(1, args.demos + 1)}
        strays = sorted(p.name for p in rec.iterdir()
                        if p.is_dir() and p.name.startswith("task") and p.name not in valid)
        if strays:
            shown = ", ".join(strays[:8]) + (" ..." if len(strays) > 8 else "")
            print(f"\nNote: {len(strays)} folder(s) not in the checklist "
                  f"(test runs / extra demos): {shown}")


if __name__ == "__main__":
    main()
