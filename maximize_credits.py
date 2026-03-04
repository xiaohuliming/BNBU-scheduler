import argparse
import os
import re
import json
import subprocess
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import pandas as pd

# -----------------------------
# Time parsing helpers
# -----------------------------
DAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
REV_DAY = {v: k for k, v in DAY_MAP.items()}
Meeting = Tuple[int, int, int]  # (day_index, start_min, end_min)


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_schedule(s: str) -> Optional[Meeting]:
    """
    Example: 'Mon 15:00-16:50' or 'Wed 8:00-9:50'
    """
    s = str(s).strip()
    m = re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$", s)
    if not m:
        return None
    day = DAY_MAP[m.group(1)]
    start = to_minutes(m.group(2))
    end = to_minutes(m.group(3))
    return (day, start, end)


def overlap(a: Meeting, b: Meeting) -> bool:
    if a[0] != b[0]:
        return False
    return a[1] < b[2] and b[1] < a[2]


def has_conflict(existing: List[Meeting], new_meetings: List[Meeting]) -> bool:
    for e in existing:
        for n in new_meetings:
            if overlap(e, n):
                return True
    return False


def fmt_meeting(m: Meeting) -> str:
    d, s, e = m
    return f"{REV_DAY[d]} {s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d}"


# -----------------------------
# Excel loading (xls/xlsx)
# -----------------------------
def convert_xls_to_xlsx_if_needed(path: str) -> str:
    """
    If input is .xls and pandas can't read due to missing xlrd,
    this function tries to convert via libreoffice (if installed).
    Returns path to readable xlsx.
    """
    if not path.lower().endswith(".xls"):
        return path

    out_dir = os.path.join(os.path.dirname(path), "_converted_xlsx")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".xlsx")

    if os.path.exists(out_path):
        return out_path

    # Try libreoffice headless conversion
    # Command: libreoffice --headless --convert-to xlsx --outdir <out_dir> <path>
    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "xlsx", "--outdir", out_dir, path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        raise RuntimeError(
            "读取 .xls 失败，且无法用 libreoffice 自动转换。\n"
            "解决方案：\n"
            "1) 安装 xlrd: pip install xlrd\n"
            "或\n"
            "2) 手动把 .xls 另存为 .xlsx\n"
            f"原始错误：{e}"
        )

    if not os.path.exists(out_path):
        raise RuntimeError("libreoffice 转换似乎没有生成 xlsx 文件，请检查 libreoffice 是否可用。")

    return out_path


def load_timetable(path: str) -> pd.DataFrame:
    """
    Your file's first row is a header row inside the sheet, so we need to "re-header".
    """
    path = convert_xls_to_xlsx_if_needed(path)

    raw = pd.read_excel(path, sheet_name=0)
    header = raw.iloc[0].tolist()
    df = raw.iloc[1:].copy()
    df.columns = header
    df = df.reset_index(drop=True)
    return df


def parse_session_from_title(title: str) -> Optional[str]:
    """
    Example: 'Advanced Accounting I (1001)' -> '1001'
    """
    m = re.search(r"\((\d+)\)\s*$", str(title).strip())
    return m.group(1) if m else None


# -----------------------------
# Core optimization (brute force with pruning)
# -----------------------------
def maximize_credits(
    df: pd.DataFrame, 
    target_codes: List[str], 
    time_range: Optional[Tuple[int, int]] = None,
    blocked_slots: Optional[List[Tuple[int, int, int]]] = None,
    teacher_constraints: Optional[Dict[str, str]] = None
) -> Dict:
    df = df[df["Course Code"].isin(target_codes)].copy()
    df["Session"] = df["Course Title & Session"].map(parse_session_from_title)
    df["Meeting"] = df["Class Schedule"].map(parse_schedule)

    min_t, max_t = time_range if time_range else (0, 24 * 60)
    blocks = blocked_slots if blocked_slots else []
    teacher_reqs = teacher_constraints if teacher_constraints else {}

    # Group into "options": one course code has multiple sessions; each session has multiple meeting rows
    options: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for (cc, sess), g in df.groupby(["Course Code", "Session"], dropna=False):
        if sess is None or (isinstance(sess, float) and pd.isna(sess)):
            continue

        meetings = [m for m in g["Meeting"].tolist() if m is not None]
        if len(meetings) == 0:
            continue

        # Check teacher constraint
        teacher = str(g["Teachers"].iloc[0]) if "Teachers" in g.columns else ""
        if cc in teacher_reqs and teacher_reqs[cc]:
            # Simple substring match (case insensitive)
            if teacher_reqs[cc].lower() not in teacher.lower():
                continue

        # Check time constraints
        is_in_range = True
        for m in meetings:
            # m = (day, start, end)
            
            # 1. Global time range check
            if m[1] < min_t or m[2] > max_t:
                is_in_range = False
                break
            
            # 2. Blocked slots check
            for b in blocks:
                b_day, b_start, b_end = b
                if m[0] == b_day:
                    # Check overlap: max(start1, start2) < min(end1, end2)
                    if max(m[1], b_start) < min(m[2], b_end):
                        is_in_range = False
                        break
            if not is_in_range:
                break
        
        if not is_in_range:
            continue

        units = int(g["Units"].iloc[0])
        title = str(g["Course Title & Session"].iloc[0])
        teacher = str(g["Teachers"].iloc[0]) if "Teachers" in g.columns else ""
        category = str(g["Course Category"].iloc[0]) if "Course Category" in g.columns else ""
        room = str(g["Room"].iloc[0]) if "Room" in g.columns else ""
        remark = str(g["Remark"].iloc[0]) if "Remark" in g.columns else ""

        options[cc][str(sess)] = {
            "course_code": cc,
            "session": str(sess),
            "title": title,
            "teacher": teacher,
            "category": category,
            "room": room,
            "remark": remark,
            "units": units,
            "meetings": meetings,
        }

    # Some courses might not exist in the file
    existing_courses = [cc for cc in target_codes if cc in options]
    if not existing_courses:
        return {"best_units": 0, "best_choice": {}, "missing": target_codes}

    max_units = {cc: max(opt["units"] for opt in options[cc].values()) for cc in existing_courses}
    # Search order: high credit & more sessions first
    courses_sorted = sorted(existing_courses, key=lambda cc: (-max_units[cc], -len(options[cc])))

    # Upper bound for pruning
    rem_ub = [0] * (len(courses_sorted) + 1)
    for i in range(len(courses_sorted) - 1, -1, -1):
        rem_ub[i] = rem_ub[i + 1] + max_units[courses_sorted[i]]

    best_units = -1
    best_solutions: List[Dict[str, str]] = []

    def dfs(i: int, current_units: int, current_meetings: List[Meeting], current_choice: Dict[str, str]):
        nonlocal best_units, best_solutions

        # prune: strict inequality because if equal, we might find another solution with same units
        if current_units + rem_ub[i] < best_units:
            return

        if i == len(courses_sorted):
            if current_units > best_units:
                best_units = current_units
                best_solutions = [current_choice.copy()]
            elif current_units == best_units:
                best_solutions.append(current_choice.copy())
            return

        cc = courses_sorted[i]

        # Option 0: skip this course
        dfs(i + 1, current_units, current_meetings, current_choice)

        # Option 1..k: choose a session
        for sess, opt in options[cc].items():
            if not has_conflict(current_meetings, opt["meetings"]):
                current_choice[cc] = sess
                dfs(
                    i + 1,
                    current_units + opt["units"],
                    current_meetings + opt["meetings"],
                    current_choice,
                )
                current_choice.pop(cc, None)

    dfs(0, 0, [], {})

    missing = [cc for cc in target_codes if cc not in options]
    
    # Reconstruct details for all solutions
    all_picked_solutions = []
    for sol in best_solutions:
        picked = []
        for cc, sess in sol.items():
            picked.append(options[cc][sess])
        picked.sort(key=lambda x: x["course_code"])
        all_picked_solutions.append(picked)

    return {
        "best_units": best_units,
        "solutions": all_picked_solutions,
        "missing": missing,
    }


# -----------------------------
# HTML Visualization
# -----------------------------
def generate_combined_html(solutions: List[List[Dict]], total_units: int) -> str:
    # 1. Calculate global time range across ALL solutions
    min_time = 8 * 60
    max_time = 22 * 60
    
    for picked in solutions:
        for p in picked:
            for m in p["meetings"]:
                min_time = min(min_time, m[1])
                max_time = max(max_time, m[2])

    # Padding
    min_time = (min_time // 60) * 60
    max_time = ((max_time // 60) + 1) * 60
    
    duration = max_time - min_time
    px_per_min = 2
    height = duration * px_per_min
    
    # 2. Prepare JS data
    js_data = []
    colors = [
        ("#e3f2fd", "#1565c0"), ("#f3e5f5", "#7b1fa2"), ("#e8f5e9", "#2e7d32"),
        ("#fff3e0", "#ef6c00"), ("#fce4ec", "#c2185b"), ("#e0f7fa", "#006064"),
        ("#fff8e1", "#f57f17"), ("#ffebee", "#c62828")
    ]
    
    for sol_idx, picked in enumerate(solutions):
        events = []
        course_details = []
        
        for i, course in enumerate(picked):
            bg, border = colors[i % len(colors)]
            cc = course['course_code']
            title = course['title']
            sess = course['session']
            teacher = course.get('teacher', '')
            category = course.get('category', '')
            room = course.get('room', '')
            remark = course.get('remark', '')
            units = course.get('units', 0)
            
            # Format time for table
            sorted_meetings = sorted(course['meetings'])
            time_lines = [fmt_meeting(m) for m in sorted_meetings]
            time_str_full = "<br>".join(time_lines)
            
            course_details.append({
                "idx": i + 1,
                "category": category,
                "code": cc,
                "title": title,
                "teacher": teacher,
                "time": time_str_full,
                "room": room,
                "units": units,
                "remark": remark,
                "bg": bg,
                "border": border
            })

            for m in course['meetings']:
                day_idx, start, end = m
                events.append({
                    "day": day_idx,
                    "start": start,
                    "end": end,
                    "code": cc,
                    "session": sess,
                    "title": title,
                    "teacher": teacher,
                    "bg": bg,
                    "border": border,
                    "timeStr": f"{start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d}"
                })
        js_data.append({
            "id": sol_idx + 1,
            "events": events,
            "details": course_details
        })

    json_str = json.dumps(js_data)
    
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    html = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        f"<title>Schedule Options ({total_units} Units)</title>",
        "<style>",
        "body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f5f5f5; padding: 20px; }",
        ".container { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 1200px; margin: 0 auto; }",
        ".controls { text-align: center; margin-bottom: 20px; padding: 10px; background: #fafafa; border-radius: 4px; border: 1px solid #eee; }",
        "select { padding: 8px 16px; font-size: 16px; border-radius: 4px; border: 1px solid #ccc; cursor: pointer; }",
        "h1 { text-align: center; color: #333; margin-top: 0; }",
        ".calendar-wrapper { position: relative; width: 100%; border: 1px solid #ddd; margin-top: 20px; overflow-y: auto; }",
        f".calendar {{ position: relative; width: 100%; height: {height + 40}px; background: white; }}",
        ".header-row { display: flex; height: 40px; background: #fafafa; border-bottom: 1px solid #ddd; position: sticky; top: 0; z-index: 10; }",
        ".time-col-header { width: 60px; border-right: 1px solid #ddd; flex-shrink: 0; }",
        ".day-header { flex: 1; text-align: center; line-height: 40px; font-weight: bold; border-right: 1px solid #ddd; color: #555; }",
        ".day-header:last-child { border-right: none; }",
        ".time-labels { position: absolute; left: 0; top: 0; width: 60px; height: 100%; border-right: 1px solid #ddd; background: #fafafa; }",
        ".time-label { position: absolute; width: 100%; text-align: right; padding-right: 5px; box-sizing: border-box; font-size: 12px; color: #999; border-top: 1px solid #eee; }",
        ".event { position: absolute; width: 92%; left: 4%; border-radius: 4px; padding: 4px; font-size: 11px; box-sizing: border-box; overflow: hidden; cursor: pointer; transition: transform 0.1s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; flex-direction: column; justify-content: center; }",
        ".event:hover { transform: scale(1.02); z-index: 5; }",
        ".event-code { font-weight: bold; margin-bottom: 2px; }",
        ".event-time { font-size: 10px; opacity: 0.8; }",
        
        "/* Table styles */",
        ".details-section { margin-top: 30px; }",
        ".details-header { background: #e3f2fd; color: #1565c0; padding: 10px; font-weight: bold; text-align: center; border: 1px solid #ddd; border-bottom: none; border-radius: 4px 4px 0 0; }",
        "table { width: 100%; border-collapse: collapse; font-size: 14px; background: white; }",
        "th, td { border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }",
        "th { background: #f9f9f9; color: #333; font-weight: bold; text-align: center; }",
        "tr:nth-child(even) { background-color: #fafafa; }",
        ".td-center { text-align: center; }",
        ".color-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }",
        "</style>",
        "</head>",
        "<body>",
        "<div class='container'>",
        f"<h1>Optimal Schedules - Total Units: {total_units}</h1>",
        
        "<div class='controls'>",
        "<label for='opt-select'><strong>Choose Option: </strong></label>",
        "<select id='opt-select' onchange='renderOption(this.value)'>",
    ]
    
    for i in range(len(solutions)):
        html.append(f"<option value='{i}'>Option {i+1}</option>")
    
    html.append("</select>")
    html.append("</div>") # controls
    
    html.append("<div class='calendar-wrapper'>")
    html.append("<div class='header-row'>")
    html.append("<div class='time-col-header'></div>")
    for d in days:
        html.append(f"<div class='day-header'>{d}</div>")
    html.append("</div>") # header-row
    
    html.append(f"<div class='calendar' id='calendar-grid'>")
    
    # Time labels
    html.append("<div class='time-labels'>")
    for t in range(min_time, max_time, 60):
        h = t // 60
        top = (t - min_time) * px_per_min
        html.append(f"<div class='time-label' style='top: {top}px'>{h:02d}:00</div>")
    html.append("</div>")
    
    # Events container (empty initially)
    html.append("<div id='events-layer'></div>")
    
    html.append("</div>") # calendar
    html.append("</div>") # calendar-wrapper
    
    # Table Section
    html.append("<div class='details-section'>")
    html.append("<div class='details-header'>Sections</div>")
    html.append("<table>")
    html.append("<thead><tr>")
    html.append("<th style='width: 40px;'>#</th>")
    html.append("<th>Course Category</th>")
    html.append("<th>Course Code</th>")
    html.append("<th>Name</th>")
    html.append("<th>Teachers</th>")
    html.append("<th>Time</th>")
    html.append("<th>Room</th>")
    html.append("<th style='width: 50px;'>Units</th>")
    html.append("<th>Remark</th>")
    html.append("</tr></thead>")
    html.append("<tbody id='details-body'></tbody>")
    html.append("</table>")
    html.append("</div>") # details-section
    
    html.append("</div>") # container
    
    # Scripts
    html.append("<script>")
    html.append(f"const solutions = {json_str};")
    html.append(f"const minTime = {min_time};")
    html.append(f"const pxPerMin = {px_per_min};")
    
    html.append("""
    function renderOption(idx) {
        const sol = solutions[idx];
        
        // Render Calendar
        const container = document.getElementById('events-layer');
        container.innerHTML = '';
        
        sol.events.forEach(ev => {
            const top = (ev.start - minTime) * pxPerMin;
            const height = (ev.end - ev.start) * pxPerMin;
            
            const div = document.createElement('div');
            div.className = 'event';
            div.style.top = top + 'px';
            div.style.height = height + 'px';
            div.style.backgroundColor = ev.bg;
            div.style.borderLeft = '3px solid ' + ev.border;
            div.style.color = ev.border;
            div.style.left = `calc(60px + ${ev.day} * ((100% - 60px) / 7))`;
            div.style.width = `calc((100% - 60px) / 7 - 4px)`;
            
            div.title = `${ev.title}\\n${ev.teacher}\\n${ev.timeStr}`;
            
            div.innerHTML = `
                <div class='event-code'>${ev.code} (${ev.session})</div>
                <div class='event-time'>${ev.timeStr}</div>
                <div style='font-size:10px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;'>${ev.title}</div>
            `;
            
            container.appendChild(div);
        });
        
        // Render Table
        const tbody = document.getElementById('details-body');
        tbody.innerHTML = '';
        
        sol.details.forEach(row => {
            const tr = document.createElement('tr');
            
            tr.innerHTML = `
                <td class='td-center'>${row.idx}</td>
                <td>${row.category}</td>
                <td>
                    <span class='color-dot' style='background-color:${row.border};'></span>
                    ${row.code}
                </td>
                <td>${row.title}</td>
                <td>${row.teacher}</td>
                <td>${row.time}</td>
                <td>${row.room}</td>
                <td class='td-center'>${row.units}</td>
                <td>${row.remark}</td>
            `;
            tbody.appendChild(tr);
        });
    }
    
    // Initial render
    renderOption(0);
    """)
    html.append("</script>")
    
    html.append("</body></html>")
    
    return "\n".join(html)


# -----------------------------
# CLI
# -----------------------------
def main():
    parser = argparse.ArgumentParser(description="Maximize credits without timetable conflicts (choose best sessions).")
    parser.add_argument("--file", required=True, help="Path to Course List and Timetable .xls or .xlsx")
    parser.add_argument("--courses", nargs="+", required=True, help="Course codes to consider, e.g. AI1013 AI3013 ...")
    args = parser.parse_args()

    df = load_timetable(args.file)
    result = maximize_credits(df, args.courses)

    print("\n==============================")
    print(f"Max Credits (no conflict): {result['best_units']}")
    solutions = result.get("solutions", [])
    print(f"Found {len(solutions)} optimal combination(s).")
    print("==============================\n")

    if result.get("missing"):
        print("Not found in file (ignored):", ", ".join(result["missing"]))
        print()

    for i, picked in enumerate(solutions):
        print(f"--- Option {i + 1} ---")
        for opt in picked:
            print(f"{opt['course_code']}  Session {opt['session']}  Units {opt['units']}")
            print(f"  Title   : {opt['title']}")
            if opt.get("teacher"):
                print(f"  Teacher : {opt['teacher']}")
            print("  Time    :")
            for m in sorted(opt["meetings"]):
                print(f"    - {fmt_meeting(m)}")
            print()
        
    # Generate Combined HTML
    if solutions:
        html_content = generate_combined_html(solutions, result['best_units'])
        out_name = "schedule_results.html"
        with open(out_name, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"Saved interactive calendar to: {os.path.abspath(out_name)}")
        print("\n")

    print("Done.")


if __name__ == "__main__":
    main()
