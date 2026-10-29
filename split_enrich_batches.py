"""Split course_catalog.json into compact per-batch input files for the AI
enrichment workflow. Each batch is dept-homogeneous (better context for the
agent's 'similar course' reasoning) and holds ~BATCH_SIZE courses.

Writes:
  enrich_work/batches/batch_000.json ...   (compact input, ~small)
  enrich_work/manifest.json                (list of batch files + codes)

Partial refresh (e.g. after the catalog descriptions changed):
  python split_enrich_batches.py --codes enrich_work/refresh/codes.txt --work enrich_work/refresh
batches only the listed codes (one per line) into <work>/batches + <work>/manifest.json.
Small dept chunks are packed together so a scattered subset doesn't explode into
one tiny batch per dept; merge the outputs with `merge_enrichment.py --work <work> --update`.
"""

import argparse
import json
import os
import re
from data_paths import data_path

BATCH_SIZE = 10
CATALOG = data_path("course_catalog.json")
WORK = "enrich_work"
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="only batch these codes (file, one code per line)")
    ap.add_argument("--work", default=WORK, help="work dir for batches/ + manifest.json")
    args = ap.parse_args()

    cat = json.load(open(CATALOG, encoding="utf-8"))
    batch_dir = os.path.join(args.work, "batches")
    os.makedirs(batch_dir, exist_ok=True)

    selected = sorted(cat)
    if args.codes:
        wanted = {ln.strip() for ln in open(args.codes, encoding="utf-8") if ln.strip()}
        unknown = sorted(wanted - set(cat))
        if unknown:
            raise SystemExit(f"codes not in {CATALOG}: {unknown[:20]}")
        selected = sorted(wanted)

    # group by dept, then chunk
    by_dept = {}
    for code in selected:
        by_dept.setdefault(cat[code]["dept"], []).append(code)

    batches = []
    for dept in sorted(by_dept):
        codes = by_dept[dept]
        for i in range(0, len(codes), BATCH_SIZE):
            chunk = codes[i:i + BATCH_SIZE]
            # subset mode: pack small chunks into the first batch that still has room
            room = None
            if args.codes:
                room = next((b for b in batches if len(b) + len(chunk) <= BATCH_SIZE), None)
            if room is not None:
                room.extend(chunk)
            else:
                batches.append(chunk)

    manifest = []
    for idx, codes in enumerate(batches):
        fname = f"batch_{idx:03d}.json"
        payload = {"batch": idx, "courses": [compact(cat, c) for c in codes]}
        with open(os.path.join(batch_dir, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        manifest.append({"batch": idx, "file": os.path.join(batch_dir, fname), "codes": codes})

    with open(os.path.join(args.work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"courses : {len(selected)}")
    print(f"batches : {len(batches)} (size {BATCH_SIZE})")
    print(f"manifest: {os.path.join(args.work, 'manifest.json')}")
    # rough size of one batch file
    sample = os.path.join(batch_dir, "batch_000.json")
    print(f"sample batch bytes: {os.path.getsize(sample)}")


if __name__ == "__main__":
    main()
