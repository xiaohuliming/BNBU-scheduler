"""Merge enrich_work/out/batch_*.json into course_enrichment.json (keyed by code).

Validates each batch output and reports missing / invalid / incomplete batches so
they can be re-run. Safe to run repeatedly while the enrichment workflow is still
in progress: it merges whatever is ready.
"""

import json
import os

WORK = "enrich_work"
OUT_DIR = os.path.join(WORK, "out")
MANIFEST = os.path.join(WORK, "manifest.json")
CATALOG = "course_catalog.json"
RESULT = "course_enrichment.json"

# Fields the UI relies on structurally; everything else is optional prose that we
# coerce to a sensible default if an agent occasionally omits it.
ESSENTIAL = ["code", "tagline", "summary_zh", "topics", "difficulty", "similar"]
OPTIONAL_STR = ["workload", "career", "further_study", "recommend_for", "tips", "caution"]


def valid_obj(o, valid_codes):
    if not isinstance(o, dict) or not o.get("code"):
        return False
    if any(k not in o for k in ESSENTIAL):
        return False
    if not isinstance(o.get("difficulty"), dict) or "level" not in o["difficulty"]:
        return False
    if not isinstance(o.get("topics"), list):
        return False
    for s in o.get("similar", []):
        if not isinstance(s, dict) or s.get("code") not in valid_codes:
            return False
    return True


def normalize(o):
    for key in OPTIONAL_STR:
        if not isinstance(o.get(key), str):
            o[key] = ""
    return o


def main():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    valid_codes = set(catalog)

    enrichment = {}
    missing, invalid, incomplete = [], [], []

    for entry in manifest:
        idx = entry["batch"]
        expected = entry["codes"]
        path = os.path.join(OUT_DIR, f"batch_{idx:03d}.json")
        if not os.path.exists(path):
            missing.append(idx)
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            invalid.append(idx)
            continue
        if not isinstance(data, list):
            invalid.append(idx)
            continue

        got = {}
        for o in data:
            if valid_obj(o, valid_codes) and o["code"] in expected:
                got[o["code"]] = normalize(o)
        if len(got) < len(expected):
            incomplete.append((idx, len(got), len(expected)))
        enrichment.update(got)

    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(enrichment, f, ensure_ascii=False, indent=1)

    total = len(catalog)
    print(f"enriched courses : {len(enrichment)}/{total}")
    print(f"missing batches  : {len(missing)} {missing[:20]}")
    print(f"invalid batches  : {len(invalid)} {invalid[:20]}")
    print(f"incomplete       : {len(incomplete)} {incomplete[:20]}")
    print(f"wrote {RESULT}")

    # emit the list of batch ids that still need (re)running
    todo = sorted(set(missing) | set(i for i in invalid) | set(i for i, _, _ in incomplete))
    if todo:
        print(f"TODO batches ({len(todo)}): {todo}")


if __name__ == "__main__":
    main()
