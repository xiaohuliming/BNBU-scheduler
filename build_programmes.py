"""Parse BNBU programme handbooks (four-year study plans) into
programme_requirements.json for the 专业修读地图 / graduation-check feature.

Source: the ECM handbook dump. TCC note: `cp -R` the folder to /tmp/hb first —
never point python at ~/Documents directly.

Each handbook PDF has:
  page(s) with a study-plan grid: sections "I. Major Required Courses (54 Units)"
  followed by course rows whose unit number sits in the (Year, Sem) column it is
  planned for; and optionally an "ME Course List" page (major-elective pool).

Output schema (programme_requirements.json):
{
  "<ABBR>": {
    "name": "...", "faculty": "FST|FBM|FHSS|SCC", "department": "...",
    "cohorts": {
      "2023": {
        "sections": [
          {"numeral": "I", "title": "Major Required Courses", "units": 54,
           "courses": [{"code","title","units","plan":[[year,sem],...]}, ...],
           "pool": [{"code","title","units"}, ...]   # elective pool if present
          }, ...
        ],
        "total_units": 158
      }, ...
    }
  }, ...
}
"""

import json
import os
import re
import unicodedata

from pypdf import PdfReader

HB_ROOT = "/tmp/hb"
OUT = "programme_requirements.json"

FACULTY_SHORT = {
    "Faculty of Science and Technology": "FST",
    "Faculty of Business and Management": "FBM",
    "Faculty of Humanities and Social Sciences": "FHSS",
    "School of Culture and Creativity": "SCC",
}

CODE_RE = re.compile(r"^([A-Z]{2,4}\d{4})\b")
SECTION_RE = re.compile(r"^\s*([IVX]+)\.\s*(.+?)\s*\(\s*(\d+)\s*Units?\s*\)", re.IGNORECASE)
FILE_RE = re.compile(r"^(.*?)\s+Programme\s*-\s*([A-Z]+)\s+(\d{4})")
POOL_HEADER_RE = re.compile(r"(ME|Major Elective)\s+Course List", re.IGNORECASE)


def clean_title(text):
    # strip footnote markers (circled digits, #, *) and squeeze spaces
    text = "".join(ch for ch in text if not ("①" <= ch <= "⑳"))
    text = text.replace("#", "").replace("*", "")
    return " ".join(text.split()).strip()


def sem_columns(line):
    """Positions of the 8 'Sem N' headers on a grid page."""
    return [m.start() for m in re.finditer(r"Sem\s*\d", line)]


def parse_grid_page(text, sections, columns_state):
    """Parse one study-plan grid page; append into sections list."""
    lines = text.splitlines()
    columns = columns_state.get("columns")
    for line in lines:
        if "Sem" in line and len(sem_columns(line)) >= 4:
            cols = sem_columns(line)
            if len(cols) >= 6:
                columns = cols
                columns_state["columns"] = cols
            continue

        m = SECTION_RE.match(line)
        if m:
            sections.append({
                "numeral": m.group(1),
                "title": " ".join(m.group(2).split()),
                "units": int(m.group(3)),
                "courses": [],
                "pool": [],
            })
            continue

        cm = CODE_RE.match(line.strip())
        if cm and sections:
            code = cm.group(1)
            rest_start = line.find(code) + len(code)
            # title runs until the first semester column (or unit tokens)
            title_end = columns[0] - 2 if columns else len(line)
            title = clean_title(line[rest_start:title_end])
            # unit tokens beyond the title area
            plan = []
            units = 0
            for tm in re.finditer(r"\d+(?:\.\d+)?", line[rest_start:]):
                pos = rest_start + tm.start()
                if columns and pos < columns[0] - 3:
                    continue  # part of the title (e.g. "5G Networks")
                val = float(tm.group(0))
                if val > 12:
                    continue  # year like 2023 etc.
                units = units or val
                if columns:
                    nearest = min(range(len(columns)), key=lambda i: abs(columns[i] - pos))
                    plan.append([nearest // 2 + 1, nearest % 2 + 1])
            if title:
                sections[-1]["courses"].append({
                    "code": code,
                    "title": title,
                    "units": int(units) if units and units == int(units) else units,
                    "plan": plan,
                })


def parse_pool_page(text):
    """Parse an 'ME Course List' page into a pool of {code,title,units}."""
    pool = []
    for line in text.splitlines():
        s = line.strip()
        cm = CODE_RE.match(s)
        if not cm:
            continue
        code = cm.group(1)
        rest = s[len(code):].strip()
        um = re.search(r"(\d+(?:\.\d+)?)\s*$", rest)
        units = float(um.group(1)) if um else 3
        title = clean_title(rest[:um.start()] if um else rest)
        if title:
            pool.append({
                "code": code,
                "title": title,
                "units": int(units) if units == int(units) else units,
            })
    return pool


def parse_handbook(path):
    reader = PdfReader(path)
    sections = []
    columns_state = {}
    pool = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:  # noqa: BLE001 - a few handbooks have a broken trailing page
            continue
        if POOL_HEADER_RE.search(text):
            pool.extend(parse_pool_page(text))
        else:
            parse_grid_page(text, sections, columns_state)
    # attach the pool to the major-elective section if there is one
    if pool:
        target = next((s for s in sections if "elective" in s["title"].lower() and "major" in s["title"].lower()), None)
        if target is not None:
            seen = {c["code"] for c in target["pool"]}
            target["pool"].extend(p for p in pool if p["code"] not in seen and not seen.add(p["code"]))
    return sections


def main():
    programmes = {}
    problems = []
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


if __name__ == "__main__":
    main()
