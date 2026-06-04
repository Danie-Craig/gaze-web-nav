#!/usr/bin/env python3
"""
generate_configs.py
Extract the 15 held-out task configs from test.raw.json,
replace URL placeholders with localhost URLs, and fix auth paths.
"""
import json, os

SHOPPING_URL = "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8082"
AUTH_DIR     = "/data/webarena-setup/webarena-tasks/.auth"
TEST_RAW     = "/data/webarena-setup/webarena-tasks/config_files/test.raw.json"
OUTPUT_DIR   = "/home/liralab-widowx/gaze-web-nav/evaluation/task_configs"

HELD_OUT_IDS = {146, 518, 352, 49, 48, 147, 240, 261, 436, 521, 796, 510, 233, 691, 351}

URL_MAP = {
    "__SHOPPING__"       : SHOPPING_URL,
    "__SHOPPING_ADMIN__" : "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8083",
    "__REDDIT__"         : "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8080",
    "__GITLAB__"         : "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:9001",
    "__WIKIPEDIA__"      : "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:8081",
    "__MAP__"            : "http://liralabwidowx-alienware-aurora-r16.tail4d611e.ts.net:443",
}

def patch_url(s):
    for placeholder, url in URL_MAP.items():
        s = s.replace(placeholder, url)
    return s

def patch_task(task):
    task = dict(task)
    # Fix start URL
    if task.get("start_url"):
        task["start_url"] = patch_url(task["start_url"])
    # Fix auth path
    if task.get("storage_state"):
        basename = os.path.basename(task["storage_state"])
        task["storage_state"] = os.path.join(AUTH_DIR, basename)
    # Fix eval reference URLs
    if task.get("eval", {}).get("reference_url"):
        task["eval"]["reference_url"] = patch_url(task["eval"]["reference_url"])
    # Fix program_html urls
    for item in task.get("eval", {}).get("program_html", []):
        if item.get("url"):
            item["url"] = patch_url(item["url"])
    return task

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(TEST_RAW) as f:
        all_tasks = json.load(f)

    count = 0
    for task in all_tasks:
        if task["task_id"] not in HELD_OUT_IDS:
            continue
        patched = patch_task(task)
        out = os.path.join(OUTPUT_DIR, f"{task['task_id']}.json")
        with open(out, "w") as f:
            json.dump(patched, f, indent=2)
        print(f"  {task['task_id']:4d}: {task['eval']['eval_types']} — {task['intent'][:60]}")
        count += 1

    print(f"\nGenerated {count} configs → {OUTPUT_DIR}/")

if __name__ == "__main__":
    main()
