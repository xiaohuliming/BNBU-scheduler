"""Build course_textbooks.json from the per-semester official textbook list.

cp the source into /tmp/srcdata first (macOS TCC: never point python at
~/Downloads or ~/Documents directly):
  /tmp/srcdata/textbook.xls   "Textbook Information for Sem N of AY20XX-YY_*.xls(x)"

The workbook has 'Required Courses ' and 'Elective Courses' sheets with the
header on the second row; column names drift between semesters ('Course Name'
vs 'Course Title', 'Category' vs 'Course Category', 'Editon' typo vs
'Edition') — all variants are accepted.
"""

import json
import re

import pandas as pd

SRC = "/tmp/srcdata/textbook.xls"
OUT = "course_textbooks.json"
SHEETS = ["Required Courses ", "Elective Courses"]
CODE_RE = re.compile(r"^[A-Z]{2,4}\d{4}$")


def clean(x):
    s = " ".join(str(x).split()).strip()
    return "" if s.lower() == "nan" else s


def pick(row, *names):
    for n in names:
        if n in row.index:
            v = clean(row[n])
            if v:
                return v
    return ""


def main():
    textbooks = {}
    for sheet in SHEETS:
        df = pd.read_excel(SRC, sheet_name=sheet, header=1)
        df.columns = [str(c).strip() for c in df.columns]
        for _, row in df.iterrows():
            code = pick(row, "Course Code")
            title = pick(row, "Book Title")
            if not CODE_RE.match(code) or not title:
                continue
            entry = {
                "title": title,
                "author": pick(row, "Author(s)", "Author"),
                "publisher": pick(row, "Publisher"),
                "edition": pick(row, "Editon", "Edition"),
                "category": pick(row, "Category", "Course Category"),
            }
            if all(e["title"] != entry["title"] for e in textbooks.get(code, [])):
                textbooks.setdefault(code, []).append(entry)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(textbooks, f, ensure_ascii=False, indent=1)
    print(f"textbooks: {len(textbooks)} courses, "
          f"{sum(len(v) for v in textbooks.values())} books -> {OUT}")

    try:
        cat = json.load(open("course_catalog.json", encoding="utf-8"))
        offered = {c for c in cat if cat[c]["offered"]}
        print(f"offered courses covered: {len(set(textbooks) & offered)} / {len(offered)}")
    except OSError:
        pass


if __name__ == "__main__":
    main()
