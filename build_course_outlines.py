"""Course outlines from past iSpace lecture titles -> course_outlines.json.

Source: the iSpace courseware metadata export (courseware_metadata.sqlite3, one
row per Moodle file: course shell name, filename, mimetype, ...). Copy it into
/tmp/srcdata/ first (macOS TCC) and pass the path explicitly.

Raw filenames can carry student IDs, name lists, seating plans etc., so they
never leave enrich_work/ (gitignored). Only the AI-written Chinese outline +
topics end up in the committed course_outlines.json.

  1. python build_course_outlines.py extract --db /tmp/srcdata/courseware_metadata.sqlite3
     -> enrich_work/outline/batches/batch_NNN.json + manifest.json
  2. AI: one subagent per batch writes enrich_work/outline/out/batch_NNN.json,
     a list of {"code", "topics": [3-8], "outline": [3-20]} (Chinese)
     or {"code", "skip": true} when the titles are too thin to ground anything.
  3. python build_course_outlines.py merge
     -> course_outlines.json (validated; prints batch ids that still need work)

Per-teacher outlines (courses offered now that 2+ teachers have taught) follow the
same three steps with extract-teachers / merge-teachers, working in
enrich_work/outline_teachers/ and writing course_teacher_outlines.json. Each AI
output item is {"code", "same": true} when all teachers cover the same ground, or
{"code", "teachers": [{"teacher", "outline": [...], "focus": "..."} | {"teacher", "skip": true}]}.
"""

import argparse
import collections
import json
import os
import re
import sqlite3
from data_paths import data_path

CATALOG = data_path("course_catalog.json")
RESULT = data_path("course_outlines.json")
WORK = os.path.join("enrich_work", "outline")
BATCH_SIZE = 20

SHELL_RE = re.compile(
    r"^(?P<title>.+?)\s*(?:\((?:Term \d)\)\s*)?\((?P<sess>\d{4})\)\s*\((?P<teacher>[^)]*)\)\s*\[(?P<sem>[^\]]+)\]"
)
CODE_RE = re.compile(r"(?<![A-Z])([A-Z]{2,4})\s?(\d{4})(?!\d)")
SEM_RE = re.compile(r"(Semester|Sem)\s*(1|2|I{1,2})\s+of\s+(\d{4})-(\d{4})", re.I)

SLIDE_MIME = ("presentation", "powerpoint")
DOC_MIME = ("application/pdf", "wordprocessingml", "application/msword")
LECTURE_RE = re.compile(
    r"(lecture|\blect?\s?\d|\bweek\s?\d|chapter|\bch\.?\s?\d|\btopic\s?\d|\bunit\s?\d|\bsession\s?\d|\bmodule\s?\d"
    r"|\bclass\s?\d|第.{1,3}[章讲课周节])",
    re.I,
)
# Assessment / admin files: not course content, and the usual home of student data.
SKIP_RE = re.compile(
    r"(assignment|homework|\bhw\s?\d|quiz|exam|mid-?term|\bfinal\b|\btest\b|solution|answer|rubric|marking|grade|score"
    r"|syllabus|outline|timetable|schedule|seating|name\s?list|namelist|roster|student|group|consultation|notice"
    r"|announcement|template|sample|guideline|guidance|instruction|requirement|project|report|essay|feedback|survey"
    r"|attendance|question|exercise|practice|worksheet|作业|考试|期中|期末|答案|练习|名单|座位|成绩|分组|通知|模板|要求|大纲)",
    re.I,
)
# Words that only number or version a file, stripped before judging informativeness.
NOISE_RE = re.compile(
    r"\b(lecture|lect|lec|week|wk|chapter|ch|topic|unit|session|module|class|part|slides?|ppt|pptx|pdf|notes?|handouts?"
    r"|updated|update|revised|new|final|copy|version|v\d+|ispace|sem\s?\d|s\d|semester|ay|for|students?"
    r"|with|and|of|the|to|in|on|an|accessible|lr|en|cn)\b",
    re.I,
)
PII_RE = re.compile(r"\d{6,}|@|1[3-9]\d{9}")


def norm_title(text):
    text = text.lower().replace("&", "and")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def semester_key(label):
    """'Semester 1 of 2025-2026' -> '2526S1' (sortable); None for summer/other."""
    m = SEM_RE.search(label or "")
    if not m:
        return None
    term = "1" if m.group(2).upper() in ("1", "I") else "2"
    return f"{m.group(3)[2:]}{m.group(4)[2:]}S{term}"


def session_index(catalog):
    """(semester key, normalised title, session) -> code, from the timetables we already ship.

    Exact per-section lookup disambiguates same-titled courses (ACCT2003 vs ACCT2043).
    """
    index = {}
    current = None
    if os.path.exists(data_path("semesters_index.json")):
        keys = [s["key"] for s in json.load(open(data_path("semesters_index.json"), encoding="utf-8"))]
        for key in keys:
            path = data_path(f"course_semester_{key}.json")
            if not os.path.exists(path):
                continue
            for course in json.load(open(path, encoding="utf-8")):
                for d in course.get("details", []):
                    m = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", str(d.get("Course Title & Session", "")))
                    if m:
                        index[(key, norm_title(m.group(1)), m.group(2))] = d.get("Course Code") or course["code"]
        if keys:
            y1, y2, term = int(keys[0][:2]), int(keys[0][2:4]), keys[0][-1]
            current = f"{y1:02d}{y2:02d}S2" if term == "1" else f"{y1 + 1:02d}{y2 + 1:02d}S1"
    for code, entry in catalog.items():
        if current and entry.get("offered"):
            for s in entry.get("sessions") or []:
                index.setdefault((current, norm_title(entry["title"]), str(s.get("session"))), code)
    return index


def map_shells(con, catalog):
    """Course shell id -> (code, semester key, teacher) for shells we can tie to one catalog code."""
    by_title = collections.defaultdict(set)
    for code, entry in catalog.items():
        for key in ("title", "title_en"):
            if entry.get(key):
                by_title[norm_title(entry[key])].add(code)
    sessions = session_index(catalog)
    shells = {}
    for cid, name in con.execute("select course_id, course_shortname from course_summary"):
        name = name or ""
        sem = semester_key(name)
        if not sem:
            continue
        code, teacher = None, None
        m = SHELL_RE.match(name)
        if m:
            code = sessions.get((sem, norm_title(m["title"]), m["sess"]))
            codes = by_title.get(norm_title(m["title"]))
            if not code and codes and len(codes) == 1:
                code = next(iter(codes))
            if code not in catalog:
                code = None
            teacher = " ".join(m["teacher"].split()) or None
        if not code:
            for prefix, digits in CODE_RE.findall(name):
                if prefix + digits in catalog:
                    code = prefix + digits
                    break
        if code:
            shells[cid] = (code, sem, teacher)
    return shells


def clean_filename(filename, code):
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename)
    stem = stem.replace("_", " ").replace("‐", "-").replace("–", "-")
    stem = re.sub(rf"\b{code[:-4]}\s?{code[-4:]}\b", " ", stem, flags=re.I)
    stem = re.sub(r"\(\d+\)$", " ", stem.strip())
    stem = re.sub(r"\b20\d{2}(?:[-/]\d{2,4})?\b", " ", stem)
    return " ".join(stem.split()).strip(" -.")[:90]


def informative(title, course_words=()):
    """True if the title names something beyond numbering and the course's own name."""
    rest = NOISE_RE.sub(" ", title)
    rest = re.sub(r"[\d\W_]+", " ", rest)
    words = [w for w in rest.split() if len(w) > 1 and w.lower() not in course_words]
    cjk = re.findall(r"[一-鿿]", title)
    return len(words) >= 2 or len(cjk) >= 2


def natural_key(title):
    m = re.search(r"\d+", title)
    return (int(m.group()) if m else 10**6, title.lower())


def collect_titles(con, shells, catalog):
    """code -> [(sem, titles, teacher)] per shell, keeping lecture-like content files only."""
    per_shell = collections.defaultdict(list)
    query = (
        "select course_id, filename, mimetype from files "
        "where component in ('mod_resource','mod_folder') and filearea='content' and filename<>'.' "
        "order by timecreated"
    )
    for cid, filename, mime in con.execute(query):
        if cid not in shells or not filename:
            continue
        mime = mime or ""
        is_slide = any(k in mime for k in SLIDE_MIME)
        is_doc = any(k in mime for k in DOC_MIME)
        if not (is_slide or (is_doc and LECTURE_RE.search(filename))):
            continue
        if SKIP_RE.search(filename) or PII_RE.search(filename):
            continue
        code = shells[cid][0]
        title = clean_filename(filename, code)
        course_words = set(norm_title(catalog[code]["title"]).split())
        if title and informative(title, course_words):
            per_shell[cid].append(title)

    by_code = collections.defaultdict(list)
    for cid, titles in per_shell.items():
        seen, uniq = set(), []
        for t in titles:
            key = re.sub(r"\W+", "", t.lower())
            if key not in seen:
                seen.add(key)
                uniq.append(t)
        code, sem, teacher = shells[cid]
        by_code[code].append((sem, sorted(uniq, key=natural_key), teacher))
    return by_code


def pick_versions(versions, max_versions=3, max_titles=40):
    """Prefer recent, well-populated shells; drop near-duplicate sections of one offering."""
    versions = sorted(versions, key=lambda v: (v[0], len(v[1])), reverse=True)
    picked = []
    for sem, titles, *_ in versions:
        if len(titles) < 3:
            continue
        sig = set(re.sub(r"\W+", "", t.lower()) for t in titles)
        if any(len(sig & p[2]) / max(1, len(sig | p[2])) > 0.6 for p in picked):
            continue
        picked.append((sem, titles[:max_titles], sig))
        if len(picked) >= max_versions:
            break
    return [{"semester": sem, "titles": titles} for sem, titles, _ in picked]


def extract(args):
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    shells = map_shells(con, catalog)
    by_code = collect_titles(con, shells, catalog)

    courses = []
    for code in sorted(by_code):
        versions = pick_versions(by_code[code])
        if not versions:
            continue
        sems = sorted({sem for sem, titles, _ in by_code[code] if len(titles) >= 3})
        c = catalog[code]
        courses.append({
            "code": code,
            "title": c["title"],
            "dept": c["dept"],
            "description": (c.get("description") or "")[:400],
            "semesters": sems,
            "versions": versions,
        })

    batch_dir = os.path.join(args.work, "batches")
    os.makedirs(batch_dir, exist_ok=True)
    for old in os.listdir(batch_dir):
        os.remove(os.path.join(batch_dir, old))
    manifest = []
    for idx in range(0, len(courses), BATCH_SIZE):
        chunk = courses[idx:idx + BATCH_SIZE]
        n = idx // BATCH_SIZE
        path = os.path.join(batch_dir, f"batch_{n:03d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"batch": n, "courses": chunk}, f, ensure_ascii=False, indent=1)
        manifest.append({"batch": n, "file": path, "codes": [c["code"] for c in chunk],
                         "semesters": {c["code"]: c["semesters"] for c in chunk}})
    with open(os.path.join(args.work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    offered = {k for k, v in catalog.items() if v.get("offered")}
    print(f"mapped shells   : {len(shells)}")
    print(f"courses w/ titles: {len(courses)} ({len({c['code'] for c in courses} & offered)} offered now)")
    print(f"batches         : {len(manifest)} (size {BATCH_SIZE}) -> {batch_dir}")


TEXT_MAX = 40


def valid_items(items, lo, hi):
    if not isinstance(items, list) or not lo <= len(items) <= hi:
        return False
    for s in items:
        if not isinstance(s, str) or not s.strip() or len(s) > TEXT_MAX or PII_RE.search(s):
            return False
    return True


def merge(args):
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    manifest = json.load(open(os.path.join(args.work, "manifest.json"), encoding="utf-8"))
    result, missing, invalid, incomplete = {}, [], [], []
    skipped = 0
    for entry in manifest:
        idx = entry["batch"]
        path = os.path.join(args.work, "out", f"batch_{idx:03d}.json")
        if not os.path.exists(path):
            missing.append(idx)
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            invalid.append(idx)
            continue
        if not isinstance(data, list):
            invalid.append(idx)
            continue
        got = 0
        for o in data:
            code = o.get("code") if isinstance(o, dict) else None
            if code not in entry["codes"] or code not in catalog:
                continue
            if o.get("skip") is True:  # titles too thin to say anything grounded
                skipped += 1
                got += 1
                continue
            if not (valid_items(o.get("topics"), 3, 8) and valid_items(o.get("outline"), 3, 20)):
                continue
            result[code] = {
                "topics": [s.strip() for s in o["topics"]],
                "outline": [s.strip() for s in o["outline"]],
                "semesters": entry["semesters"][code],
            }
            got += 1
        if got < len(entry["codes"]):
            incomplete.append((idx, got, len(entry["codes"])))

    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(result.items())), f, ensure_ascii=False, indent=1)
    print(f"outlined courses: {len(result)} (skipped as too thin: {skipped})")
    print(f"missing batches : {len(missing)} {missing[:20]}")
    print(f"invalid batches : {len(invalid)} {invalid[:20]}")
    print(f"incomplete      : {len(incomplete)} {incomplete[:20]}")
    print(f"wrote {RESULT}")
    todo = sorted(set(missing) | set(invalid) | {i for i, _, _ in incomplete})
    if todo:
        print(f"TODO batches ({len(todo)}): {todo}")


TEACHER_RESULT = data_path("course_teacher_outlines.json")
TEACHER_WORK = os.path.join("enrich_work", "outline_teachers")
TEACHER_BATCH_SIZE = 10
MAX_TEACHERS = 4


def norm_person(name):
    """iSpace and the timetable spell names differently ("Veicheng" vs "Vei Cheng", "Dr." vs "Ms.")."""
    name = re.sub(r"^\s*(dr|prof|professor|mr|ms|mrs|miss)\.?\s+", "", (name or "").lower())
    return re.sub(r"[^a-z]", "", name)


def teacher_versions(by_code, catalog):
    """code -> per-teacher lecture titles, for courses offered now that 2+ teachers have taught."""
    result = {}
    for code, versions in by_code.items():
        entry = catalog[code]
        if not entry.get("offered"):
            continue
        current = {norm_person(t): t for t in entry.get("teachers") or []}
        per = collections.defaultdict(list)
        for sem, titles, teacher in versions:
            if teacher and len(titles) >= 3:
                per[norm_person(teacher)].append((sem, titles, teacher))
        rows = []
        for key, shells in per.items():
            sem, titles, spelled = max(shells, key=lambda v: (v[0], len(v[1])))  # latest, fullest section
            rows.append({"teacher": current.get(key, spelled), "semesters": sorted({v[0] for v in shells}),
                         "titles": titles[:40], "now": key in current})
        # Teachers on this semester's timetable first, then the most recent ones.
        rows.sort(key=lambda r: (r["now"], r["semesters"][-1], len(r["titles"])), reverse=True)
        if len(rows) >= 2:
            result[code] = rows[:MAX_TEACHERS]
    return result


def extract_teachers(args):
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    by_teacher = teacher_versions(collect_titles(con, map_shells(con, catalog), catalog), catalog)

    courses = [{
        "code": code,
        "title": catalog[code]["title"],
        "description": (catalog[code].get("description") or "")[:300],
        "teachers": [{"teacher": r["teacher"], "titles": r["titles"]} for r in rows],
    } for code, rows in sorted(by_teacher.items())]

    batch_dir = os.path.join(args.work, "batches")
    os.makedirs(batch_dir, exist_ok=True)
    for old in os.listdir(batch_dir):
        os.remove(os.path.join(batch_dir, old))
    manifest = []
    for idx in range(0, len(courses), TEACHER_BATCH_SIZE):
        chunk = courses[idx:idx + TEACHER_BATCH_SIZE]
        n = idx // TEACHER_BATCH_SIZE
        path = os.path.join(batch_dir, f"batch_{n:03d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"batch": n, "courses": chunk}, f, ensure_ascii=False, indent=1)
        manifest.append({"batch": n, "file": path, "codes": [c["code"] for c in chunk],
                         "teachers": {c["code"]: {r["teacher"]: r["semesters"] for r in by_teacher[c["code"]]}
                                      for c in chunk}})
    with open(os.path.join(args.work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    teaching_now = sum(r["now"] for rows in by_teacher.values() for r in rows)
    print(f"courses w/ 2+ teachers: {len(courses)} (teacher versions {sum(len(c['teachers']) for c in courses)}, "
          f"{teaching_now} teaching now)")
    print(f"batches               : {len(manifest)} (size {TEACHER_BATCH_SIZE}) -> {batch_dir}")


def merge_teachers(args):
    """AI output per course: {"code", "same": true} when every teacher covers the same ground, else
    {"code", "teachers": [{"teacher", "outline": [...], "focus": "..."} | {"teacher", "skip": true}]}."""
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    manifest = json.load(open(os.path.join(args.work, "manifest.json"), encoding="utf-8"))
    result, missing, invalid, incomplete = {}, [], [], []
    same = thin = 0
    for entry in manifest:
        idx = entry["batch"]
        path = os.path.join(args.work, "out", f"batch_{idx:03d}.json")
        if not os.path.exists(path):
            missing.append(idx)
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except ValueError:
            invalid.append(idx)
            continue
        if not isinstance(data, list):
            invalid.append(idx)
            continue
        got = 0
        for o in data:
            code = o.get("code") if isinstance(o, dict) else None
            if code not in entry["codes"] or code not in catalog:
                continue
            if o.get("same") is True:
                same += 1
                got += 1
                continue
            known = entry["teachers"][code]
            rows = {}
            for t in o.get("teachers") or []:
                name = t.get("teacher") if isinstance(t, dict) else None
                if name not in known or t.get("skip") is True or not valid_items(t.get("outline"), 3, 20):
                    continue
                focus = t.get("focus")
                focus = focus.strip() if isinstance(focus, str) and len(focus) <= TEXT_MAX and not PII_RE.search(focus) else ""
                rows[name] = {"teacher": name, "semesters": known[name],
                              "outline": [s.strip() for s in t["outline"]], "focus": focus}
            if not isinstance(o.get("teachers"), list):
                continue
            got += 1
            people, ordered = set(), []
            for n in known:  # keep the extract's ordering; one entry per person
                if n in rows and norm_person(n) not in people:
                    people.add(norm_person(n))
                    ordered.append(rows[n])
            sets = [set(r["outline"]) for r in ordered]
            if len(ordered) >= 2 and all(len(a & b) / len(a | b) >= 0.7 for i, a in enumerate(sets) for b in sets[i + 1:]):
                same += 1  # the AI split it, but the outlines themselves barely differ
            elif len(ordered) >= 2:
                result[code] = ordered
            else:
                thin += 1
        if got < len(entry["codes"]):
            incomplete.append((idx, got, len(entry["codes"])))

    with open(TEACHER_RESULT, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(result.items())), f, ensure_ascii=False, indent=1)
    print(f"courses w/ per-teacher outlines: {len(result)} (same content: {same}, <2 usable teachers: {thin})")
    print(f"missing batches : {len(missing)} {missing[:20]}")
    print(f"invalid batches : {len(invalid)} {invalid[:20]}")
    print(f"incomplete      : {len(incomplete)} {incomplete[:20]}")
    print(f"wrote {TEACHER_RESULT}")
    todo = sorted(set(missing) | set(invalid) | {i for i, _, _ in incomplete})
    if todo:
        print(f"TODO batches ({len(todo)}): {todo}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extract", "extract-teachers"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True, help="courseware_metadata.sqlite3 (copied to /tmp/srcdata/)")
        p.add_argument("--work", default=TEACHER_WORK if name.endswith("teachers") else WORK)
    for name in ("merge", "merge-teachers"):
        p = sub.add_parser(name)
        p.add_argument("--work", default=TEACHER_WORK if name.endswith("teachers") else WORK)
    args = ap.parse_args()
    {"extract": extract, "merge": merge,
     "extract-teachers": extract_teachers, "merge-teachers": merge_teachers}[args.cmd](args)


if __name__ == "__main__":
    main()
