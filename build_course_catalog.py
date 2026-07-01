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

import pandas as pd
from pypdf import PdfReader

from maximize_credits import load_timetable

PDF_PATH = "/Users/xhlm/Desktop/Study/大数据/小组项目/data/Course Descriptions_20260421.pdf"
XLSX_PATH = "Course List and Timetable_Semester 2 of AY2025-26_20260112.xlsx"
OUT_PATH = "course_catalog.json"

CODE_RE = re.compile(r"[A-Z]{2,4}\d{4}")


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
        if re.fullmatch(r"[IVXLC]+", w):  # roman numeral (course levels I/II/III)
            out.append(w)
        elif w.isalpha() and w.isupper() and len(w) <= 5 and not re.search(r"[AEIOU]", w):
            out.append(w)  # consonant-only acronym: CIS, IFRS, CVP, HR
        elif i > 0 and low in SMALL_WORDS:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def parse_pdf(path):
    reader = PdfReader(path)
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    text = re.sub(r"\n\s*\d+\s*/\s*\d+\s*\n", "\n", text)  # strip "12 / 172" headers

    block_re = re.compile(
        r"([A-Z]{2,4}\d{4})\s+([A-Z][A-Z0-9 &\-\(\)/,'’.:]+?)\s*\(\s*(\d+)\s*units?\s*\)\s*"
        r"Pre-?requisite\(s\)\s*:\s*(.*?)\s*Course Description\s*:\s*(.*?)"
        r"(?=[A-Z]{2,4}\d{4}\s+[A-Z][A-Z0-9 &\-\(\)/,'’.:]+?\s*\(\s*\d+\s*units?\s*\)\s*Pre-?requisite|\Z)",
        re.DOTALL,
    )
    out = {}
    for code, title, units, prereq, desc in block_re.findall(text):
        if code in out:
            continue
        prereq_clean = clean(prereq)
        # "None" / "Nil" -> empty
        prereq_codes = [c for c in CODE_RE.findall(prereq_clean) if c != code]
        out[code] = {
            "code": code,
            "title_en": title_case(clean(title)),
            "units": int(units),
            "description": clean(desc),
            "prereq_text": prereq_clean,
            "prereq_codes": sorted(set(prereq_codes)),
        }
    return out


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

        # Requirements column mixes Chinese prereqs; harvest any codes present too.
        req_zh = " / ".join(uniq("Requirements"))
        prereq_codes_zh = [c for c in CODE_RE.findall(req_zh) if c != code]

        offered[code] = {
            "code": code,
            "title": title,
            "units": int(clean(first.get("Units", "")) or 0) or None,
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

    all_codes = sorted(set(pdf) | set(xlsx))
    catalog = {}
    for code in all_codes:
        p = pdf.get(code, {})
        x = xlsx.get(code, {})
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
    print(f"total courses in catalog : {len(catalog)}")
    print(f"offered this semester    : {len(offered)}")
    print(f"offered WITH description : {len(offered_desc)}")
    print(f"offered WITHOUT desc     : {len(offered) - len(offered_desc)}")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
