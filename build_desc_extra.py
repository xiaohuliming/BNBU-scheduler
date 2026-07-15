"""Build course_descriptions_extra.json from the per-semester supplementary
description files (WPEC whole-person-education PDF + CFL language-centre PDF).

These cover courses the main Course Descriptions PDF lacks; /api/course falls
back to them when a course has no official description.

cp the sources into /tmp/srcdata first (macOS TCC: never point python at
~/Downloads or ~/Documents directly):
  /tmp/srcdata/wpec.pdf   "WPEC Course Descriptions (Semester N of 20XX-YY).pdf"
  /tmp/srcdata/cfl.pdf    "CFL FE Course Description - Sem N, 20XX-YY.pdf"
"""

import json
import re

import pdfplumber

from build_course_catalog import parse_pdf  # CFL uses the same block format

WPEC_PDF = "/tmp/srcdata/wpec.pdf"
CFL_PDF = "/tmp/srcdata/cfl.pdf"
OUT = "course_descriptions_extra.json"

CODE_RE = re.compile(r"^[A-Z]{2,4}\d{4}$")


def clean(text):
    return " ".join(str(text or "").split()).strip()


def parse_wpec(path):
    """Table layout: Course Title | Course Code | Option | Desc EN | Desc CN.
    Rows continued across pages come back without a code — merge into the
    previous entry."""
    out = {}
    last = None
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row or all(c in (None, "") for c in row):
                        continue
                    cells = [clean(c) for c in row]
                    code = next((c for c in cells if CODE_RE.match(c)), "")
                    if code:
                        # heuristics: EN description = longest latin cell,
                        # CN = longest cell containing CJK
                        latin = [c for c in cells if c and c != code and not re.search(r"[一-鿿]", c)]
                        cjk = [c for c in cells if re.search(r"[一-鿿]", c)]
                        en = max(latin, key=len) if latin else ""
                        cn = max(cjk, key=len) if cjk else ""
                        if en == "/":
                            en = ""
                        if cn == "/":
                            cn = ""
                        if len(en) < 40:
                            en = ""  # a title cell, not a description
                        out[code] = {"description": en, "description_cn": cn, "source": "WPEC"}
                        last = code
                    elif last:
                        # continuation row: extend the previous descriptions
                        for c in cells:
                            if not c or c == "/":
                                continue
                            if re.search(r"[一-鿿]", c):
                                out[last]["description_cn"] = clean(out[last]["description_cn"] + " " + c)
                            elif len(c) > 40:
                                out[last]["description"] = clean(out[last]["description"] + " " + c)
    return {k: v for k, v in out.items() if v["description"] or v["description_cn"]}


def parse_cfl(path):
    blocks = parse_pdf(path)
    return {
        code: {"description": b["description"], "description_cn": "", "source": "CFL"}
        for code, b in blocks.items() if b.get("description")
    }


def main():
    extra = {}
    extra.update(parse_wpec(WPEC_PDF))
    n_wpec = len(extra)
    for code, entry in parse_cfl(CFL_PDF).items():
        extra.setdefault(code, entry)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(extra, f, ensure_ascii=False, indent=1)
    print(f"WPEC: {n_wpec} | CFL: {len(extra) - n_wpec} | total -> {OUT}")

    # coverage: how many offered-without-description courses these fill
    try:
        cat = json.load(open("course_catalog.json", encoding="utf-8"))
        missing = {c for c in cat if cat[c]["offered"] and not cat[c]["has_description"]}
        print(f"fills offered-without-desc: {len(missing & set(extra))} / {len(missing)}")
    except OSError:
        pass


if __name__ == "__main__":
    main()
