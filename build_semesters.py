"""Build per-semester course offering JSON from historical ECM timetables.

The current semester is always served live from the timetable xlsx in the repo
root (get_df in app.py); this script only bakes PAST/OTHER semesters so the
course explorer can browse them.

TCC note: copy the source files out of ~/Documents (protected) into /tmp with
`cp` first — do NOT point python at ~/Documents directly.

Outputs (repo root):
  semesters_index.json            [{key, label}]  (recent -> old)
  course_semester_<key>.json      same shape as /api/courses response
"""

import json
import os

import pandas as pd

# (key, label, source file in /tmp/sem)
SEMESTERS = [
    ("2526S2", "25-26 第二学期", "/tmp/sem/2526S2.xlsx"),
    ("2526S1", "25-26 第一学期", "/tmp/sem/2526S1.xlsx"),
    ("2425S2", "24-25 第二学期", "/tmp/sem/2425S2.xls"),
    ("2425S1", "24-25 第一学期", "/tmp/sem/2425S1.xlsx"),
    ("2324S2", "23-24 第二学期", "/tmp/sem/2324S2.xlsx"),
]

KEEP_COLUMNS = [
    "Course Code", "Course Title & Session", "Offering Unit",
    "Offering Programme", "Units", "Curriculum Type", "Elective Type",
    "Teachers", "Class Schedule", "Hours", "Classroom", "Requirements", "Remarks",
]


def load_timetable_any(path):
    """Same re-headering as maximize_credits.load_timetable, but engine-agnostic
    (handles legacy .xls via xlrd without the libreoffice conversion step)."""
    raw = pd.read_excel(path, sheet_name=0)
    header = [str(x).strip() for x in raw.iloc[0].tolist()]
    df = raw.iloc[1:].copy()
    df.columns = header
    return df.reset_index(drop=True)


def group_courses(df):
    """Mirror /api/courses grouping so the explorer can reuse the same shape."""
    courses = []
    for code, group in df.groupby("Course Code"):
        title_full = str(group["Course Title & Session"].iloc[0])
        title = title_full.split("(")[0].strip()
        teachers = [str(t) for t in group["Teachers"].unique().tolist() if pd.notna(t)]
        details = []
        for _, row in group.iterrows():
            row_data = row.where(pd.notnull(row), "").to_dict()
            details.append({k: row_data.get(k, "") for k in KEEP_COLUMNS})
        courses.append({"code": code, "name": title, "teachers": teachers, "details": details})
    return courses


def main():
    index = []
    for key, label, src in SEMESTERS:
        if not os.path.exists(src):
            print(f"SKIP {key}: {src} not found (cp it to /tmp/sem first)")
            continue
        df = load_timetable_any(src)
        courses = group_courses(df)
        out = f"course_semester_{key}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False)
        index.append({"key": key, "label": label})
        print(f"{key} ({label}): {len(courses)} courses, {sum(len(c['details']) for c in courses)} sessions -> {out} ({os.path.getsize(out)//1024}KB)")

    with open("semesters_index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"semesters_index.json: {len(index)} semesters")


if __name__ == "__main__":
    main()
