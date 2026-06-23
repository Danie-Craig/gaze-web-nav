#!/usr/bin/env python3
"""
Split the WebArena shopping tasks into train / val / test BY TASK.

Why "by task": a task and all of its human-demo trajectories must live in the
SAME split. Splitting by trajectory or by step would leak a task the model has
practiced into validation/test and inflate the numbers. So we assign whole
task_ids to splits; collection then records N demos per train/val task.

Guarantees
----------
- Split is by task_id, never by trajectory/step  -> no task leaks across splits.
- Fixed --seed  -> fully reproducible (sort-then-shuffle, independent of file order).
- The 15 held-out evaluation task_ids are FORCED into test and NEVER appear in
  train or val.
- train + val  = the tasks you collect human demos for (--demos each).
  test          = evaluated live by automated WebArena (no demos collected).

Outputs (to --out-dir, default current dir)
-------------------------------------------
  splits.json               canonical manifest: {"train":[...], "val":[...], "test":[...], + metadata}
  collection_checklist.csv  task_id, split, demos_needed, intent   (train+val only)

COMMIT splits.json to the repo. It freezes the split forever; re-running with the
same seed reproduces it, but the committed file is the source of truth.

Usage
-----
  python split_tasks.py                         # 70/15/15, seed 42, 3 demos/task
  python split_tasks.py --train 0.60 --val 0.15 # bigger test => fewer demo tasks
  python split_tasks.py --tasks all_shopping_tasks.json --out-dir ../data
"""
import argparse
import csv
import json
import random
from pathlib import Path

# The 15 evaluation task_ids that MUST land in test (and never in train/val).
EVAL_TASK_IDS = [48, 49, 146, 147, 233, 240, 261, 351, 352, 436, 510, 518, 521, 691, 796]


def load_tasks(path):
    """Return {task_id: intent}. Accepts a top-level list or a dict wrapping one."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for k in ("tasks", "data", "items"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            raise SystemExit(f"Could not find a task list inside {path} (top-level keys: {list(data)})")
    tasks = {}
    for t in data:
        if "task_id" not in t:
            raise SystemExit(f"A task entry is missing 'task_id': {str(t)[:120]}")
        tasks[int(t["task_id"])] = t.get("intent", "")
    if not tasks:
        raise SystemExit(f"No tasks loaded from {path}")
    return tasks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="all_shopping_tasks.json", help="path to the task list JSON")
    ap.add_argument("--out-dir", default=".", help="where to write splits.json + checklist")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--train", type=float, default=0.70, help="train fraction of all tasks")
    ap.add_argument("--val", type=float, default=0.15, help="val fraction of all tasks (test = remainder)")
    ap.add_argument("--demos", type=int, default=3, help="human demos to collect per train/val task")
    args = ap.parse_args()

    if args.train + args.val >= 1.0:
        raise SystemExit("--train + --val must be < 1.0 (test is the remainder)")

    tasks = load_tasks(args.tasks)
    all_ids = sorted(tasks)                       # deterministic regardless of file order
    n = len(all_ids)

    # Every forced eval id must actually exist in the dataset.
    missing = [i for i in EVAL_TASK_IDS if i not in tasks]
    if missing:
        raise SystemExit(f"These forced eval IDs are NOT present in {args.tasks}: {missing}")

    eval_set = set(EVAL_TASK_IDS)
    pool = [i for i in all_ids if i not in eval_set]     # free to assign

    # Target sizes over the WHOLE dataset.
    n_train = round(args.train * n)
    n_val = round(args.val * n)
    n_test = n - n_train - n_val

    if n_test < len(eval_set):
        print(f"NOTE: target test size ({n_test}) < forced eval IDs ({len(eval_set)}); "
              f"test will be enlarged to {len(eval_set)} to fit them.")

    # test already holds the forced eval IDs; fill the rest from the shuffled pool.
    n_test_extra = max(0, n_test - len(eval_set))

    rng = random.Random(args.seed)
    shuffled = pool[:]
    rng.shuffle(shuffled)

    test_extra = shuffled[:n_test_extra]
    rest = shuffled[n_test_extra:]
    train = rest[:n_train]
    val = rest[n_train:n_train + n_val]
    leftover = rest[n_train + n_val:]                    # rounding remainder (usually empty) -> test

    train = sorted(train)
    val = sorted(val)
    test = sorted(eval_set | set(test_extra) | set(leftover))

    # ---- invariants: fail loudly rather than ship a bad split ----
    assert len(train) + len(val) + len(test) == n, "splits do not sum to N"
    assert not (set(train) & set(val)), "train/val overlap"
    assert not (set(train) & set(test)), "train/test overlap"
    assert not (set(val) & set(test)), "val/test overlap"
    assert eval_set <= set(test), "a forced eval ID escaped test"
    assert not (eval_set & set(train)), "a forced eval ID leaked into train"
    assert not (eval_set & set(val)), "a forced eval ID leaked into val"

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "seed": args.seed,
        "n_tasks": n,
        "fractions": {"train": args.train, "val": args.val,
                      "test": round(1 - args.train - args.val, 4)},
        "counts": {"train": len(train), "val": len(val), "test": len(test)},
        "demos_per_task": args.demos,
        "forced_eval_ids_in_test": sorted(eval_set),
        "train": train,
        "val": val,
        "test": test,
    }
    (out / "splits.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    with open(out / "collection_checklist.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["task_id", "split", "demos_needed", "intent"])
        for split_name, ids in (("train", train), ("val", val)):
            for i in ids:
                w.writerow([i, split_name, args.demos, tasks[i]])

    demo_tasks = len(train) + len(val)
    print(f"Loaded {n} tasks from {args.tasks}  (seed={args.seed})")
    print(f"  train: {len(train):>3}  ({100*len(train)/n:4.1f}%)")
    print(f"  val  : {len(val):>3}  ({100*len(val)/n:4.1f}%)")
    print(f"  test : {len(test):>3}  ({100*len(test)/n:4.1f}%)   incl. {len(eval_set)} forced eval IDs")
    print(f"\nDemos to collect: {demo_tasks} tasks x {args.demos} = {demo_tasks*args.demos} "
          f"trajectories (train+val only).")
    print(f"Test = automated WebArena eval, no demos.")
    print(f"\nWrote:\n  {out/'splits.json'}\n  {out/'collection_checklist.csv'}")


if __name__ == "__main__":
    main()
