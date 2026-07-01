"""Split course_catalog.json into compact per-batch input files for the AI
enrichment workflow. Each batch is dept-homogeneous (better context for the
agent's 'similar course' reasoning) and holds ~BATCH_SIZE courses.

Writes:
  enrich_work/batches/batch_000.json ...   (compact input, ~small)
  enrich_work/manifest.json                (list of batch files + codes)
"""

import json
import os
import re

BATCH_SIZE = 10
CATALOG = "course_catalog.json"
WORK = "enrich_work"
BATCH_DIR = os.path.join(WORK, "batches")
DESC_CAP = 700


def compact(cat, code):
    c = cat[code]

    def resolve(codes, cap=10):
        out = []
        for x in codes[:cap]:
            if x in cat:
                out.append({"code": x, "title": cat[x]["title"]})
            else:
                out.append({"code": x, "title": ""})
        return out

    # candidate pool for "similar": dept peers + prereqs + unlocks, deduped
    cand = []
    seen = set()
    for x in (c["similar"] + c["prereq_codes"] + c["unlocks"]):
        if x != code and x not in seen and x in cat:
            seen.add(x)
            cand.append({"code": x, "title": cat[x]["title"]})
        if len(cand) >= 12:
            break

    return {
        "code": code,
        "title": c["title"],
        "title_en": c["title_en"],
        "units": c["units"],
        "dept": c["dept"],
        "level": c["level"],
        "offered": c["offered"],
        "offering_units": c["offering_units"],
        "offering_programmes": c["offering_programmes"],
        "years": c["years"],
        "description": (c["description"] or "")[:DESC_CAP],
        "prereq_text": c["prereq_text"],
        "prereqs": resolve(c["prereq_codes"]),
        "unlocks": resolve(c["unlocks"]),
        "similar_candidates": cand,
    }


def main():
    cat = json.load(open(CATALOG, encoding="utf-8"))
    os.makedirs(BATCH_DIR, exist_ok=True)

    # group by dept, then chunk
    by_dept = {}
    for code in sorted(cat):
        by_dept.setdefault(cat[code]["dept"], []).append(code)

    batches = []
    for dept in sorted(by_dept):
        codes = by_dept[dept]
        for i in range(0, len(codes), BATCH_SIZE):
            batches.append(codes[i:i + BATCH_SIZE])

    manifest = []
    for idx, codes in enumerate(batches):
        fname = f"batch_{idx:03d}.json"
        payload = {"batch": idx, "courses": [compact(cat, c) for c in codes]}
        with open(os.path.join(BATCH_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        manifest.append({"batch": idx, "file": os.path.join(BATCH_DIR, fname), "codes": codes})

    with open(os.path.join(WORK, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"courses : {len(cat)}")
    print(f"batches : {len(batches)} (size {BATCH_SIZE})")
    print(f"manifest: {os.path.join(WORK, 'manifest.json')}")
    # rough size of one batch file
    sample = os.path.join(BATCH_DIR, "batch_000.json")
    print(f"sample batch bytes: {os.path.getsize(sample)}")


if __name__ == "__main__":
    main()
