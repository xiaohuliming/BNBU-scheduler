"""Where the curated datasets live.

Course catalog, AI enrichment, outlines, SkillPath, semester timetables, the
campus knowledge index and the timetable workbook are kept in the private
`private_data` git submodule (github.com/xiaohuliming/maxcourse-data), not in
this public repo, so they cannot be cloned in bulk. Fetch them with

    git submodule update --init private_data

(needs access to the private repo). Without it the app still starts and the
data-backed features degrade to empty results.
"""

import glob
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "private_data")


def data_path(*parts):
    return os.path.join(DATA_DIR, *parts)


def timetable_workbook():
    """The current semester's official "Course List and Timetable" workbook, or None."""
    files = [f for f in glob.glob(data_path("*.xlsx")) + glob.glob(data_path("*.xls"))
             if not os.path.basename(f).startswith("~$")]
    for f in sorted(files):
        if "Course List" in os.path.basename(f):
            return f
    return sorted(files)[0] if files else None
