"""Derive course equivalence pairs from the catalog's own prerequisite texts.

Three institutional signals, in decreasing strength (no transitive closure —
only direct symmetric pairs are stored, to avoid over-merging):

1. Mutual-exclusion lists: "未曾修读过X" in a course's Chinese requirements
   means X overlaps this course so much that both cannot be counted — the
   registry's own substitution relation (e.g. DS1013 excludes AI1003/GCIT1023).
2. EN slash groups: "ACCT2003/ACCT2043 PRINCIPLES OF ACCOUNTING I" — one title,
   two codes = cross-listed equivalents.
3. ZH 或者-alternative groups, but ONLY pairs where at least one code is absent
   from the catalog (legacy renumbering, e.g. GCLA1903 ↔ UCLC1013). Alternative
   prereqs between two current courses are often merely "either background is
   sufficient", not equivalence — those are skipped.

Output: course_equivalences.json  {code: [equivalent codes...]}  (symmetric)
"""

import json
import re

CATALOG = "course_catalog.json"
OUT = "course_equivalences.json"

CODE = r"[A-Z]{2,4}\d{4}"


def main():
    cat = json.load(open(CATALOG, encoding="utf-8"))
    pairs = set()

    def add(a, b, source):
        if a != b:
            pairs.add((min(a, b), max(a, b), source))

    for code, c in cat.items():
        zh = c.get("prereq_text_zh", "") or ""
        en = c.get("prereq_text", "") or ""

        # 1) exclusion lists -> equivalents of THIS course
        for m in re.finditer(r"未曾修读过\s*(" + CODE + r")", zh):
            add(code, m.group(1), "exclusion")

        # 2) EN slash groups
        for m in re.finditer(CODE + r"(?:\s*/\s*" + CODE + r")+", en):
            group = re.findall(CODE, m.group(0))
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    add(group[i], group[j], "slash")

        # 3) ZH 或者 groups, legacy-code pairs only
        for m in re.finditer(r"\(([^()]*或者[^()]*)\)", zh):
            group = re.findall(r"修读过\s*(" + CODE + r")", m.group(1))
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if a not in cat or b not in cat:
                        add(a, b, "legacy-alt")

    equiv = {}
    for a, b, _src in pairs:
        equiv.setdefault(a, set()).add(b)
        equiv.setdefault(b, set()).add(a)
    out = {k: sorted(v) for k, v in sorted(equiv.items())}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    by_src = {}
    for _a, _b, s in pairs:
        by_src[s] = by_src.get(s, 0) + 1
    print(f"pairs: {len(pairs)} {by_src} | codes with equivalents: {len(out)} -> {OUT}")
    for probe in ["AI1003", "GCLA1903", "ACCT2003", "UCLC1013"]:
        print(f"  {probe}: {out.get(probe, [])}")


if __name__ == "__main__":
    main()
