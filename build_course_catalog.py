"""Build course_catalog.json by merging the PDF course descriptions with the
semester timetable xlsx.

Deterministic step (no LLM). Produces the foundation the AI-enrichment workflow
and the frontend course-detail page both read.

Output shape (per course code):
    {
      "code", "title", "title_en", "units",
      "offered": bool,                     # offered this semester (in xlsx)
      "dept",                              # letter prefix, e.g. "ACCT"
      "level",                             # first digit of the number, e.g. 2/3/4
      "description",                       # from PDF
      "prereq_text",                       # raw "Pre-requisite(s)" text from PDF
      "prereq_text_zh",                    # raw "Requirements" from xlsx (Chinese)
      "prereq_codes": [...],               # course codes referenced as prereqs
      "unlocks": [...],                    # courses that list THIS as a prereq
      "similar": [...],                    # deterministic seed: same dept, near level
      "offering_units": [...],             # e.g. ["FBM"]
      "offering_programmes": [...],
      "curriculum_types": [...],           # MR / ME / GE ...
      "elective_types": [...],
      "years": [...],                      # from Remarks, e.g. ["Y3"]
      "teachers": [...],                   # unique teacher names across sessions
      "sessions": [ {session, teacher, schedule, classroom, hours, units} ]
    }
"""

import json
import re
import sys
from difflib import SequenceMatcher

import pandas as pd
from pypdf import PdfReader

from maximize_credits import load_timetable

# cp the source PDF into /tmp/srcdata first (macOS TCC: never point python at
# ~/Downloads or ~/Documents directly).
PDF_PATH = "/tmp/srcdata/Course Descriptions_20260716.pdf"
XLSX_PATH = "Course List and Timetable_Semester 1 of AY2026-27_20260709.xlsx"
OUT_PATH = "course_catalog.json"
# Codes present in the previous catalog but in neither the PDF nor the new
# timetable are carried over (offered=False) so historical-semester browsing,
# enrichment and prereq links never lose entries across semester swaps.
PREV_CATALOG = "course_catalog.json"

CODE_RE = re.compile(r"[A-Z]{2,4}\d{4}")
ZH_REQ_TOKEN_RE = re.compile(r"(未曾修读过|修读过)|([A-Z]{2,4}\d{4})")
# Timetable variants carry a 4-digit section suffix (AIM20338001 -> AIM2033).
SUFFIXED_CODE_RE = re.compile(r"([A-Z]{2,4}\d{4})\d{4}")


def zh_prereq_codes(req_zh, code):
    """Codes the Chinese Requirements text says must HAVE been taken.

    未曾修读过X means "must NOT have taken X" -- a mutually-exclusive course,
    not a prerequisite. A bare code after 或者 inherits the nearest predicate
    (修读过AI2003 或者 COMP2003)."""
    codes, predicate = [], "修读过"
    for m in ZH_REQ_TOKEN_RE.finditer(req_zh or ""):
        if m.group(1):
            predicate = m.group(1)
        elif predicate == "修读过" and m.group(2) != code:
            codes.append(m.group(2))
    return codes


def clean(text):
    return " ".join(str(text or "").split()).strip()


SMALL_WORDS = {"of", "and", "the", "for", "in", "to", "a", "an", "on", "with",
               "&", "or", "as", "at", "by", "from"}


def title_case(title):
    """UPPERCASE PDF titles -> nicer display casing, keeping roman numerals and
    obvious acronyms (short all-caps tokens with no vowels, e.g. CIS/IFRS/CVP)."""
    words = title.split()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if re.fullmatch(r"[IVXLC]+", w) or re.search(r"\d", w):  # roman numeral / 3D
            out.append(w)
        elif w.isalpha() and w.isupper() and len(w) <= 5 and not re.search(r"[AEIOU]", w):
            out.append(w)  # consonant-only acronym: CIS, IFRS, CVP, HR
        elif i > 0 and low in SMALL_WORDS:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def loose_word(word):
    """pypdf sometimes splits a word with a stray space ("Course Descript ion")."""
    return r"\s?".join(re.escape(ch) for ch in word)


def parse_pdf(path):
    reader = PdfReader(path)
    return parse_pdf_text("\n".join((p.extract_text() or "") for p in reader.pages))


def parse_pdf_text(text):
    text = re.sub(r"\n\s*\d+\s*/\s*\d+\s*\n", "\n", text)  # strip "12 / 172" headers

    # Titles are upper-case, but pypdf wraps long ones across lines and some
    # carry C++ / ? / – / ISO9001 or a stray typo (STATISTICs). So a title may
    # hold anything except the start of another "CODE TITLE" or a lower-case
    # word -- the two things that would let it run on through prose.
    code_start = r"[A-Z]{2,4}\d{4}\s+[A-Z0-9]"
    title = r"(?!" + code_start + r")[A-Z0-9](?:(?!" + code_start + r")(?![a-z]{2}).)+?"
    units_pre = (r"\s*\(\s*(\d+)\s*(?i:" + loose_word("units") + r"?)\s*\)\s*"
                 + loose_word("Pre") + r"-?\s?" + loose_word("requisite") + r"s?")
    header = r"([A-Z]{2,4}\d{4})\s+(" + title + r")" + units_pre
    next_header = r"[A-Z]{2,4}\d{4}\s+" + title + units_pre.replace(r"(\d+)", r"\d+")
    not_past_header = r"(?:(?!" + next_header + r").)*?"
    block_re = re.compile(
        header + r"(?:\(s\))?\s*[:：]\s*(" + not_past_header + r")\s*"  # some blocks use a full-width colon
        + loose_word("Course") + r"\s*" + loose_word("Description") + r"\s*[:：]\s*(.*?)(?=" + next_header + r"|\Z)",
        re.DOTALL,
    )
    marker_re = re.compile(units_pre, re.IGNORECASE)
    out = {}
    for code, title, units, prereq, desc in block_re.findall(text):
        prereq_clean = clean(prereq)
        # "None" / "Nil" -> empty; "Co-requisite(s): X" is taken alongside, not before
        required_part = re.split(r"Co-?\s?requisite", prereq_clean, flags=re.IGNORECASE)[0]
        prereq_codes = [c for c in CODE_RE.findall(required_part) if c != code]
        block = {
            "code": code,
            "title_en": title_case(clean(title)),
            "units": int(units),
            "description": clean(desc),
            "prereq_text": prereq_clean,
            "prereq_codes": sorted(set(prereq_codes)),
        }
        # Renamed courses are printed twice under one code (old + new title);
        # keep every version so the timetable title can pick the right one.
        out.setdefault(code, {**block, "versions": []})["versions"].append(block)
    # A "(n units) Pre-requisite" left inside a parsed block means a header was
    # missed and the next course got glued onto this one.
    leaked = sorted({v["code"] for e in out.values() for v in e["versions"]
                     if marker_re.search(v["description"]) or marker_re.search(v["prereq_text"])})
    versioned = sum(1 for e in out.values() if len(e["versions"]) > 1)
    print(f"pdf blocks parsed        : {sum(len(e['versions']) for e in out.values())} "
          f"(header markers in text: {len(marker_re.findall(text))}; "
          f"{len(out)} codes, {versioned} printed in two versions)")
    if leaked:
        print(f"WARNING: {len(leaked)} blocks still contain another course's header: {leaked[:20]}")
    return out


def pick_version(entry, title):
    """The PDF version of a course whose title is closest to the timetable's."""
    if not entry:
        return {}
    if not title:
        return entry["versions"][0]

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    return max(entry["versions"],
               key=lambda v: SequenceMatcher(None, norm(v["title_en"]), norm(title)).ratio())


def parse_xlsx(path):
    df = load_timetable(path)
    df = df.fillna("")
    offered = {}
    for code, group in df.groupby("Course Code"):
        code = clean(code)
        if not code:
            continue
        sessions = []
        teachers = []
        for _, row in group.iterrows():
            title_full = clean(row.get("Course Title & Session", ""))
            m = re.search(r"\((\d+)\)\s*$", title_full)
            session = m.group(1) if m else ""
            teacher = clean(row.get("Teachers", ""))
            if teacher and teacher not in teachers:
                teachers.append(teacher)
            sessions.append({
                "session": session,
                "teacher": teacher,
                "schedule": clean(row.get("Class Schedule", "")),
                "classroom": clean(row.get("Classroom", "")),
                "hours": clean(row.get("Hours", "")),
                "units": clean(row.get("Units", "")),
            })
        first = group.iloc[0]
        title_full = clean(first.get("Course Title & Session", ""))
        title = re.sub(r"\s*\(\d+\)\s*$", "", title_full)

        def uniq(col):
            vals = []
            for v in group.get(col, pd.Series(dtype=str)):
                v = clean(v)
                if v and v not in vals:
                    vals.append(v)
            return vals

        # Requirements column mixes Chinese prereqs; harvest the codes it requires.
        req_zh = " / ".join(uniq("Requirements"))
        prereq_codes_zh = zh_prereq_codes(req_zh, code)

        offered[code] = {
            "code": code,
            "title": title,
            "units": int(float(clean(first.get("Units", "")) or 0)) or None,
            "offering_units": uniq("Offering Unit"),
            "offering_programmes": uniq("Offering Programme"),
            "curriculum_types": uniq("Curriculum Type"),
            "elective_types": uniq("Elective Type"),
            "years": uniq("Remarks"),
            "prereq_text_zh": req_zh,
            "prereq_codes_zh": sorted(set(prereq_codes_zh)),
            "teachers": teachers,
            "sessions": sessions,
        }
    return offered


def main():
    pdf = parse_pdf(PDF_PATH)
    xlsx = parse_xlsx(XLSX_PATH)

    try:
        with open(PREV_CATALOG, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}

    all_codes = sorted(set(pdf) | set(xlsx) | set(prev))
    catalog = {}
    carried = []
    for code in all_codes:
        if code not in pdf and code not in xlsx:
            entry = dict(prev[code])
            entry.update(offered=False, sessions=[], teachers=[], unlocks=[], similar=[])
            # re-derive from the stored texts so old parser mistakes don't linger
            en_codes = [c for c in CODE_RE.findall(entry.get("prereq_text", "")) if c != code]
            zh_codes = zh_prereq_codes(entry.get("prereq_text_zh", ""), code)
            entry["prereq_codes"] = sorted(set(en_codes) | set(zh_codes))
            catalog[code] = entry
            carried.append(code)
            continue
        x = xlsx.get(code, {})
        p = pick_version(pdf.get(code), x.get("title"))
        dept = re.match(r"[A-Z]+", code)
        num = re.search(r"\d", code)
        prereq_codes = sorted(set(p.get("prereq_codes", [])) | set(x.get("prereq_codes_zh", [])))
        catalog[code] = {
            "code": code,
            "title": x.get("title") or p.get("title_en") or code,
            "title_en": p.get("title_en", ""),
            "units": x.get("units") or p.get("units"),
            "offered": code in xlsx,
            "has_description": code in pdf,
            "dept": dept.group(0) if dept else "",
            "level": int(code[num.start()]) if num else None,
            "description": p.get("description", ""),
            "prereq_text": p.get("prereq_text", ""),
            "prereq_text_zh": x.get("prereq_text_zh", ""),
            "prereq_codes": prereq_codes,
            "unlocks": [],
            "similar": [],
            "offering_units": x.get("offering_units", []),
            "offering_programmes": x.get("offering_programmes", []),
            "curriculum_types": x.get("curriculum_types", []),
            "elective_types": x.get("elective_types", []),
            "years": x.get("years", []),
            "teachers": x.get("teachers", []),
            "sessions": x.get("sessions", []),
        }

    # Section variants (AIM20338001) have no PDF block of their own; borrow the
    # base course's description (the version whose title matches) so their
    # detail page isn't empty.
    borrowed, renamed = [], []
    for code, c in catalog.items():
        m = SUFFIXED_CODE_RE.fullmatch(code)
        if c["description"] or not m or m.group(1) not in pdf:
            continue
        base = pick_version(pdf[m.group(1)], c["title"])
        c.update(description=base["description"], description_from=m.group(1), has_description=True)
        borrowed.append(code)
        if re.sub(r"\W", "", c["title"].lower()) != re.sub(r"\W", "", base["title_en"].lower()):
            renamed.append(f"{code} {c['title']!r} <- {m.group(1)} {base['title_en']!r}")

    # reverse prereq graph: who unlocks off this course
    for code, c in catalog.items():
        for pre in c["prereq_codes"]:
            if pre in catalog:
                catalog[pre]["unlocks"].append(code)
    for c in catalog.values():
        c["unlocks"] = sorted(set(c["unlocks"]))

    # deterministic "similar" seed: same dept, level within +-1, prefer offered
    by_dept = {}
    for code, c in catalog.items():
        by_dept.setdefault(c["dept"], []).append(code)
    for code, c in catalog.items():
        peers = []
        for other in by_dept.get(c["dept"], []):
            if other == code:
                continue
            oc = catalog[other]
            if c["level"] and oc["level"] and abs(oc["level"] - c["level"]) <= 1:
                peers.append(other)
        peers.sort(key=lambda o: (not catalog[o]["offered"], o))
        c["similar"] = peers[:8]

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)

    offered = [c for c in catalog.values() if c["offered"]]
    offered_desc = [c for c in offered if c["has_description"]]
    print(f"carried from prev catalog: {len(carried)} (not offered, kept for history/links)")
    print(f"total courses in catalog : {len(catalog)}")
    print(f"offered this semester    : {len(offered)}")
    print(f"offered WITH description : {len(offered_desc)}")
    print(f"offered WITHOUT desc     : {len(offered) - len(offered_desc)}")
    print(f"variants borrowing base description: {len(borrowed)}")
    for line in renamed:
        print(f"  title differs from base, check: {line}")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
