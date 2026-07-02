"""Parse BNBU programme handbooks (four-year study plans) into
programme_requirements.json for the 专业地图 / graduation-check feature.

Source: the ECM handbook dump. TCC note: `cp -R` the folder to /tmp/hb first —
never point python at ~/Documents directly.

Handbooks mark PLAN REVISIONS with red strikethrough text: struck-out courses
were REMOVED from the plan (their replacements appear in red, not struck). The
text layer alone cannot see this, so parsing uses pdfplumber geometry: a course
row (or a unit cell) is dropped when a horizontal line/thin rect passes through
the middle band of its text.

Output schema (programme_requirements.json):
{
  "<ABBR>": {
    "name": "...", "faculty": "FST|FBM|FHSS|SCC", "department": "...",
    "cohorts": {
      "2023": {
        "sections": [
          {"numeral": "I", "title": "Major Required Courses", "units": 54,
           "courses": [{"code","title","units","plan":[[year,sem],...]}, ...],
           "pool": [{"code","title","units"}, ...]},
          ...
        ],
        "total_units": 148
      }, ...
    }
  }, ...
}
"""

import json
import os
import re

import pdfplumber

HB_ROOT = "/tmp/hb"
OUT = "programme_requirements.json"

FACULTY_SHORT = {
    "Faculty of Science and Technology": "FST",
    "Faculty of Business and Management": "FBM",
    "Faculty of Humanities and Social Sciences": "FHSS",
    "School of Culture and Creativity": "SCC",
}

CODE_RE = re.compile(r"^[A-Z]{2,4}\d{4}$")
SECTION_RE = re.compile(r"^\s*([IVX]+)\.\s*(.+?)\s*\(\s*(\d+)\s*Units?\s*\)", re.IGNORECASE)
FILE_RE = re.compile(r"^(.*?)\s+Programme\s*-\s*([A-Z]+)\s+(\d{4})")
POOL_HEADER_RE = re.compile(r"(ME|Major Elective)\s+Course List", re.IGNORECASE)
NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def clean_title(text):
    text = "".join(ch for ch in text if not ("①" <= ch <= "⑳"))
    text = text.replace("#", "").replace("*", "")
    return " ".join(text.split()).strip()


def strike_segments(page):
    """Horizontal line/thin-rect segments that could be strikethroughs."""
    segs = []
    for l in page.lines:
        if abs(l["top"] - l["bottom"]) <= 0.8 and (l["x1"] - l["x0"]) > 4:
            segs.append((l["x0"], l["x1"], (l["top"] + l["bottom"]) / 2))
    for r in page.rects:
        if (r["bottom"] - r["top"]) <= 2.5 and (r["x1"] - r["x0"]) > 4:
            segs.append((r["x0"], r["x1"], (r["top"] + r["bottom"]) / 2))
    return segs


def make_struck(segs):
    def struck(w):
        h = w["bottom"] - w["top"]
        lo, hi = w["top"] + 0.2 * h, w["top"] + 0.8 * h
        for x0, x1, y in segs:
            if lo < y < hi:
                overlap = min(w["x1"], x1) - max(w["x0"], x0)
                if overlap > 0.5 * (w["x1"] - w["x0"]):
                    return True
        return False
    return struck


def group_rows(words, tolerance=3.0):
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def parse_grid_page(page, sections, columns_state):
    words = page.extract_words()
    struck = make_struck(strike_segments(page))
    for row in group_rows(words):
        texts = [w["text"] for w in row]
        joined = " ".join(texts)

        sem_anchors = [w["x0"] for w in row if w["text"] == "Sem"]
        if len(sem_anchors) >= 6:
            columns_state["columns"] = sem_anchors
            continue

        m = SECTION_RE.match(joined)
        if m:
            sections.append({
                "numeral": m.group(1),
                "title": " ".join(m.group(2).split()),
                "units": int(m.group(3)),
                "courses": [],
                "pool": [],
            })
            continue

        first = row[0]
        if not (CODE_RE.match(first["text"]) and sections):
            continue
        if struck(first):
            continue  # course removed from the plan (strikethrough)

        columns = columns_state.get("columns")
        first_col = columns[0] if columns else None
        title_words, plan, units = [], [], 0
        for w in row[1:]:
            if NUM_RE.match(w["text"]) and (first_col is None or w["x0"] >= first_col - 6):
                if struck(w):
                    continue  # this planned placement was struck out
                val = float(w["text"])
                if val > 12:
                    continue
                units = units or val
                if columns:
                    center = (w["x0"] + w["x1"]) / 2
                    nearest = min(range(len(columns)), key=lambda i: abs(columns[i] - center))
                    plan.append([nearest // 2 + 1, nearest % 2 + 1])
            elif first_col is None or w["x0"] < first_col - 2:
                title_words.append(w["text"])
        title = clean_title(" ".join(title_words))
        if title:
            sections[-1]["courses"].append({
                "code": first["text"],
                "title": title,
                "units": int(units) if units and units == int(units) else (units or 3),
                "plan": plan,
            })


def parse_pool_page(page):
    pool = []
    struck = make_struck(strike_segments(page))
    for row in group_rows(page.extract_words()):
        first = row[0]
        if not CODE_RE.match(first["text"]):
            continue
        if struck(first):
            continue
        nums = [w for w in row[1:] if NUM_RE.match(w["text"]) and float(w["text"]) <= 12]
        units = float(nums[-1]["text"]) if nums else 3
        title_words = [w["text"] for w in row[1:] if w not in nums[-1:]]
        title = clean_title(" ".join(title_words))
        if title:
            pool.append({
                "code": first["text"],
                "title": title,
                "units": int(units) if units == int(units) else units,
            })
    return pool


def parse_handbook(path):
    sections = []
    columns_state = {}
    pool = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - some handbooks have a broken trailing page
                continue
            if POOL_HEADER_RE.search(text):
                pool.extend(parse_pool_page(page))
            else:
                parse_grid_page(page, sections, columns_state)
    if pool:
        target = next((s for s in sections if "elective" in s["title"].lower() and "major" in s["title"].lower()), None)
        if target is not None:
            seen = {c["code"] for c in target["pool"]}
            for p in pool:
                if p["code"] not in seen:
                    seen.add(p["code"])
                    target["pool"].append(p)
    return sections


def main():
    programmes = {}
    problems = []
    unit_warnings = []
    for root, _dirs, files in os.walk(HB_ROOT):
        for fn in sorted(files):
            if not fn.lower().endswith(".pdf"):
                continue
            fm = FILE_RE.match(fn)
            if not fm:
                problems.append((fn, "filename not parseable"))
                continue
            name, abbr, cohort = fm.group(1).strip(), fm.group(2), fm.group(3)
            rel = os.path.relpath(root, HB_ROOT).split(os.sep)
            faculty = FACULTY_SHORT.get(rel[1] if len(rel) > 1 else "", "")
            department = rel[2] if len(rel) > 2 else ""

            try:
                sections = parse_handbook(os.path.join(root, fn))
            except Exception as exc:  # noqa: BLE001
                problems.append((fn, f"parse error: {exc}"))
                continue

            n_courses = sum(len(s["courses"]) for s in sections)
            if len(sections) < 3 or n_courses < 10:
                problems.append((fn, f"suspicious: {len(sections)} sections, {n_courses} courses"))

            # consistency: listed units should not exceed the declared section total
            for s in sections:
                listed = sum(c["units"] for c in s["courses"])
                if s["courses"] and listed > s["units"]:
                    unit_warnings.append(f"{abbr} {cohort} {s['numeral']}.{s['title']}: listed {listed}U > declared {s['units']}U")

            entry = programmes.setdefault(abbr, {
                "name": name, "faculty": faculty, "department": department, "cohorts": {},
            })
            entry["cohorts"][cohort] = {
                "sections": sections,
                "total_units": sum(s["units"] for s in sections),
            }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(programmes, f, ensure_ascii=False)

    n_cohorts = sum(len(p["cohorts"]) for p in programmes.values())
    print(f"programmes: {len(programmes)} | cohort-plans: {n_cohorts} -> {OUT} ({os.path.getsize(OUT)//1024}KB)")
    print(f"problems: {len(problems)}")
    for fn, why in problems[:15]:
        print("  !", fn, "->", why)
    print(f"unit warnings (listed > declared): {len(unit_warnings)}")
    for w in unit_warnings[:15]:
        print("  ~", w)


if __name__ == "__main__":
    main()
