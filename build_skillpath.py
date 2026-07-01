"""Build the SkillPath layer for maxcourse from the Big-Data group project's data.

Reuses the group project's extracted data (course->skills, LinkedIn jobs) but
rebuilds clean joins around a curated, UIC-relevant set of target careers, then
emits compact artifacts that power:
  - course detail "skills" + "careers & salary" sections
  - a career-goal course recommender (Personalized PageRank, run live in Flask)

Outputs (maxcourse root):
  skillpath_courses.json   code  -> {skills:[{name,category}], careers:[{label,score,salary_median}]}
  skillpath_careers.json   label -> {faculty, skills:[{name,weight,category}], salary:{...}, n_postings, top_courses}
  skillpath_skills.json    name  -> {category, courses:[code...], careers:[label...]}
  skillpath_graph.npz      column-normalised sparse transition matrix for PPR
  skillpath_nodes.json     node index <-> id/type/label maps

Salary note: LinkedIn 2023-24 dataset (US market, USD/yr). Surfaced with that caveat.
"""

import csv
import json
import re
import sys
from collections import defaultdict

import numpy as np
from scipy import sparse

SK = "/Users/xhlm/Desktop/Study/大数据/小组项目/output"
COURSES_SKILLS = f"{SK}/courses_skills.csv"
SKILL_TAXONOMY = f"{SK}/skill_taxonomy.csv"
JOBS_SKILLS = f"{SK}/jobs_sample_skills.csv"
JOBS_CLEAN = f"{SK}/jobs_clean.csv"
CATALOG = "course_catalog.json"

# Curated target careers relevant to UIC programmes. keywords match LinkedIn
# job titles (case-insensitive substring). Grouped by faculty for the UI.
CAREERS = {
    # FST — computing / data / science
    "Data Analyst": ("FST", ["data analyst", "data analytics"]),
    "Data Scientist": ("FST", ["data scientist", "data science", "applied scientist", "machine learning scientist"]),
    "Machine Learning Engineer": ("FST", ["machine learning engineer", "ml engineer", "ai engineer", "machine learning"]),
    "Software Engineer": ("FST", ["software engineer", "software developer", "software development"]),
    "Backend Developer": ("FST", ["backend", "back-end", "back end developer"]),
    "Frontend Developer": ("FST", ["frontend", "front-end", "front end developer"]),
    "Full-Stack Developer": ("FST", ["full stack", "full-stack"]),
    "Data Engineer": ("FST", ["data engineer"]),
    "DevOps Engineer": ("FST", ["devops", "site reliability"]),
    "Business Intelligence Analyst": ("FST", ["business intelligence", "bi analyst", "bi developer"]),
    "Database Administrator": ("FST", ["database administrator", "database engineer", "database developer", "dba"]),
    "Cybersecurity Analyst": ("FST", ["security analyst", "cybersecurity", "information security"]),
    "Quantitative Analyst": ("FST", ["quantitative analyst", "quantitative researcher", "quant "]),
    "Statistician": ("FST", ["statistician", "biostatistician", "biostatistics", "statistical analyst"]),
    "Web Developer": ("FST", ["web developer", "web development", "web programmer"]),
    # FBM — business / accounting / finance / economics
    "Accountant": ("FBM", ["accountant", "accounting"]),
    "Auditor": ("FBM", ["auditor", "audit associate", "internal audit"]),
    "Financial Analyst": ("FBM", ["financial analyst", "finance analyst"]),
    "Investment Banking Analyst": ("FBM", ["investment banking", "investment banker", "investment analyst", "investment associate"]),
    "Actuary": ("FBM", ["actuary", "actuarial"]),
    "Tax Consultant": ("FBM", ["tax associate", "tax analyst", "tax consultant", "tax accountant"]),
    "Financial Advisor": ("FBM", ["financial advisor", "financial planner", "wealth"]),
    "Marketing Manager": ("FBM", ["marketing manager", "marketing specialist", "marketing coordinator"]),
    "Digital Marketing Specialist": ("FBM", ["digital marketing", "seo", "social media manager", "social media marketing"]),
    "Brand Manager": ("FBM", ["brand manager", "brand strategist", "brand marketing", "brand director"]),
    "Business Analyst": ("FBM", ["business analyst"]),
    "Management Consultant": ("FBM", ["management consultant", "strategy consultant", "consulting"]),
    "Human Resources Manager": ("FBM", ["human resources", "hr manager", "recruiter", "talent acquisition"]),
    "Supply Chain Analyst": ("FBM", ["supply chain", "logistics", "procurement"]),
    "Operations Manager": ("FBM", ["operations manager", "operations analyst"]),
    "Product Manager": ("FBM", ["product manager", "product owner"]),
    "Economist": ("FBM", ["economist", "economic analyst", "economics", "economic research"]),
    # FHSS — media / design / culture / humanities
    "Graphic Designer": ("FHSS", ["graphic designer", "graphic design"]),
    "UX/UI Designer": ("FHSS", ["ux designer", "ui designer", "user experience", "product designer", "ux/ui"]),
    "Content Writer": ("FHSS", ["content writer", "copywriter", "content creator", "content strategist"]),
    "Journalist": ("FHSS", ["journalist", "reporter", "news editor", "correspondent", "multimedia journalist"]),
    "Public Relations Specialist": ("FHSS", ["public relations", "pr specialist", "communications specialist", "communications manager"]),
    "Translator / Interpreter": ("FHSS", ["translator", "interpreter", "localization", "translation", "interpretation", "linguist"]),
    "Video Editor": ("FHSS", ["video editor", "videographer", "motion graphics", "video producer"]),
    "Media Producer": ("FHSS", ["media producer", "content producer", "film producer", "production assistant"]),
    "Counselor / Psychologist": ("FHSS", ["counselor", "counsellor", "psychologist", "therapist", "mental health"]),
    "Teacher / Educator": ("FHSS", ["teacher", "educator", "instructor", "tutor"]),
    # Cross-faculty
    "Environmental Consultant": ("Science", ["environmental", "sustainability"]),
    "Research Assistant": ("Science", ["research assistant", "research associate", "research scientist"]),
    "Project Manager": ("Cross", ["project manager", "project coordinator"]),
    "Sales Manager": ("Cross", ["sales manager", "account executive", "sales representative"]),
}

# Careers below this many skill-sample jobs get their skill profile supplemented
# from a related, data-rich career (and are flagged low_sample in the UI).
MIN_SKILL_JOBS = 8
PARENTS = {
    "Data Scientist": ["Machine Learning Engineer", "Data Analyst"],
    "Statistician": ["Data Analyst", "Data Scientist"],
    "Quantitative Analyst": ["Data Scientist", "Financial Analyst"],
    "Web Developer": ["Full-Stack Developer", "Frontend Developer"],
    "Database Administrator": ["Backend Developer", "Data Engineer"],
    "Investment Banking Analyst": ["Financial Analyst"],
    "Actuary": ["Financial Analyst", "Data Analyst"],
    "Economist": ["Financial Analyst", "Business Analyst"],
    "Brand Manager": ["Marketing Manager"],
    "Journalist": ["Content Writer"],
    "Translator / Interpreter": ["Content Writer"],
    "Video Editor": ["Media Producer"],
}


def clean(s):
    return " ".join(str(s or "").split()).strip()


def split_skills(cell):
    return [clean(x) for x in str(cell or "").split(",") if clean(x)]


def load_skill_categories():
    cat = {}
    for r in csv.DictReader(open(SKILL_TAXONOMY, encoding="utf-8")):
        name = clean(r["skill"])
        if name:
            cat.setdefault(name.lower(), r["category"])
    return cat


def median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return (s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2)


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main():
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    skill_cat = load_skill_categories()

    def category_of(name):
        return skill_cat.get(name.lower(), "Other")

    # 1. course -> skills (from group project's extraction)
    course_skills = {}
    for r in csv.DictReader(open(COURSES_SKILLS, encoding="utf-8")):
        code = clean(r["code"])
        skills = split_skills(r.get("extracted_skills"))
        if code and skills:
            course_skills[code] = skills

    # 2. jobs sample -> per-title skills; jobs_clean -> per-title salary
    job_skill_rows = [(clean(r["title"]), split_skills(r.get("extracted_skills")))
                      for r in csv.DictReader(open(JOBS_SKILLS, encoding="utf-8"))]
    job_salaries = []
    for r in csv.DictReader(open(JOBS_CLEAN, encoding="utf-8")):
        title = clean(r["title"])
        try:
            sal = float(r.get("normalized_salary") or 0)
        except ValueError:
            sal = 0
        job_salaries.append((title, sal))

    # 3. career profiles: aggregate skills (sample) + salary (clean) by keyword match
    def matches(title_lower, keywords):
        # word-boundary match so "translator" != "translational", "dba" != "adback"
        return any(re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", title_lower)
                   for kw in keywords)

    careers = {}
    for label, (faculty, keywords) in CAREERS.items():
        kws = [k.lower() for k in keywords]
        skill_freq = defaultdict(int)
        n_skill_jobs = 0
        for title, skills in job_skill_rows:
            if matches(title.lower(), kws):
                n_skill_jobs += 1
                for s in set(skills):
                    skill_freq[s] += 1
        salaries = [sal for title, sal in job_salaries if sal > 0 and matches(title.lower(), kws)]
        n_postings = sum(1 for title, _ in job_salaries if matches(title.lower(), kws))

        top = sorted(skill_freq.items(), key=lambda kv: -kv[1])[:25]
        maxf = top[0][1] if top else 1
        skills_weighted = [
            {"name": name, "weight": round(freq / maxf, 3), "category": category_of(name)}
            for name, freq in top
        ]
        careers[label] = {
            "label": label,
            "faculty": faculty,
            "n_skill_jobs": n_skill_jobs,
            "n_postings": n_postings,
            "skills": skills_weighted,
            "salary": {
                "median": round(median(salaries)) if salaries else None,
                "p25": round(percentile(salaries, 0.25)) if salaries else None,
                "p75": round(percentile(salaries, 0.75)) if salaries else None,
                "n": len(salaries),
                "currency": "USD",
            },
            "top_courses": [],
        }

    # 3b. Supplement thin careers with skills from related data-rich careers,
    #     and flag them low_sample so the UI can disclose the small sample.
    #     own_skills is a deep snapshot so discounting one career never corrupts a parent.
    own_skills = {label: [dict(s) for s in c["skills"]] for label, c in careers.items()}
    for label, c in careers.items():
        c["low_sample"] = c["n_skill_jobs"] < MIN_SKILL_JOBS
        if not c["low_sample"]:
            continue
        if c["n_skill_jobs"] < 3:
            # 1-2 postings: own skills are pure noise, rely on related careers
            c["skills"] = []
        else:
            conf = c["n_skill_jobs"] / MIN_SKILL_JOBS
            for s in c["skills"]:
                s["weight"] = round(float(s["weight"]) * conf, 3)
        have = {s["name"].lower() for s in c["skills"]}
        for parent in PARENTS.get(label, []):
            for s in own_skills.get(parent, []):
                low = s["name"].lower()
                if low in have:
                    continue
                have.add(low)
                c["skills"].append({"name": s["name"], "weight": round(float(s["weight"]) * 0.6, 3),
                                    "category": s["category"], "borrowed_from": parent})
        c["skills"] = sorted(c["skills"], key=lambda x: -x["weight"])[:18]

    # 4. course -> careers: score by shared-skill weight overlap
    #    career skill weight map for scoring
    career_skillw = {label: {s["name"].lower(): s["weight"] for s in c["skills"]}
                     for label, c in careers.items()}
    course_careers = {}
    for code, skills in course_skills.items():
        sl = {s.lower() for s in skills}
        scored = []
        for label, wmap in career_skillw.items():
            overlap = [wmap[s] for s in sl if s in wmap]
            if overlap:
                score = sum(overlap)
                scored.append((label, score, len(overlap)))
        scored.sort(key=lambda x: -x[1])
        course_careers[code] = scored[:6]

    # top_courses per career (reverse index)
    for code, scored in course_careers.items():
        for label, score, _ in scored:
            careers[label]["top_courses"].append((code, round(score, 3)))
    for c in careers.values():
        c["top_courses"] = sorted(c["top_courses"], key=lambda x: -x[1])[:12]

    # 5. skill -> {category, courses, careers}
    skill_courses = defaultdict(list)
    for code, skills in course_skills.items():
        for s in skills:
            skill_courses[s.lower()].append(code)
    skills_out = {}
    all_skill_names = {}
    for code, skills in course_skills.items():
        for s in skills:
            all_skill_names.setdefault(s.lower(), s)
    for c in careers.values():
        for s in c["skills"]:
            all_skill_names.setdefault(s["name"].lower(), s["name"])
    skill_careers = defaultdict(list)
    for label, c in careers.items():
        for s in c["skills"]:
            skill_careers[s["name"].lower()].append(label)
    for low, name in all_skill_names.items():
        skills_out[name] = {
            "category": category_of(name),
            "courses": sorted(set(skill_courses.get(low, [])))[:40],
            "careers": skill_careers.get(low, []),
        }

    # 6. Emit course/career/skill JSON
    courses_out = {}
    for code in catalog:
        skills = course_skills.get(code, [])
        courses_out[code] = {
            "skills": [{"name": s, "category": category_of(s)} for s in skills],
            "careers": [
                {"label": label, "score": round(score, 3),
                 "salary_median": careers[label]["salary"]["median"],
                 "faculty": careers[label]["faculty"]}
                for label, score, _ in course_careers.get(code, [])
            ],
        }

    json.dump(courses_out, open("skillpath_courses.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(careers, open("skillpath_careers.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(skills_out, open("skillpath_skills.json", "w", encoding="utf-8"), ensure_ascii=False)

    # 7. Build PPR graph -> column-normalised transition matrix
    #    Nodes: all catalog courses, all skills used, all careers.
    node_ids = []
    node_type = []
    node_label = []
    idx = {}

    def add_node(nid, ntype, label):
        if nid in idx:
            return idx[nid]
        idx[nid] = len(node_ids)
        node_ids.append(nid)
        node_type.append(ntype)
        node_label.append(label)
        return idx[nid]

    for code in catalog:
        add_node(f"course:{code}", "course", catalog[code]["title"])
    for low, name in all_skill_names.items():
        add_node(f"skill:{name}", "skill", name)
    for label in careers:
        add_node(f"career:{label}", "career", label)

    rows, cols, vals = [], [], []

    def add_edge(a, b, w):
        rows.append(a); cols.append(b); vals.append(w)

    # course <-> skill (both directions)
    for code, skills in course_skills.items():
        cu = idx.get(f"course:{code}")
        if cu is None:
            continue
        for s in skills:
            su = idx.get(f"skill:{s}")
            if su is None:
                continue
            add_edge(cu, su, 1.0)
            add_edge(su, cu, 1.0)
    # career <-> skill (weighted, both directions)
    for label, c in careers.items():
        ca = idx.get(f"career:{label}")
        for s in c["skills"]:
            su = idx.get(f"skill:{s['name']}")
            if su is None:
                continue
            w = float(s["weight"]) + 0.05
            add_edge(ca, su, w)
            add_edge(su, ca, w)
    # course -> prereq course (and reverse, weaker) from catalog graph
    for code, c in catalog.items():
        cu = idx.get(f"course:{code}")
        for pre in c.get("prereq_codes", []):
            pu = idx.get(f"course:{pre}")
            if pu is None:
                continue
            add_edge(cu, pu, 0.6)
            add_edge(pu, cu, 0.3)

    n = len(node_ids)
    A = sparse.csr_matrix((vals, (rows, cols)), shape=(n, n), dtype=np.float64)
    # column-normalise -> M (so M @ r conserves mass); dangling handled at run time
    colsum = np.asarray(A.sum(axis=0)).ravel()
    inv = np.divide(1.0, colsum, out=np.zeros_like(colsum), where=colsum > 0)
    D = sparse.diags(inv)
    M = (A @ D).tocsr()

    sparse.save_npz("skillpath_graph.npz", M)
    json.dump({
        "node_ids": node_ids,
        "node_type": node_type,
        "node_label": node_label,
        "course_index": { nid.split("course:", 1)[1]: i for i, nid in enumerate(node_ids) if nid.startswith("course:") },
        "career_index": { nid.split("career:", 1)[1]: i for i, nid in enumerate(node_ids) if nid.startswith("career:") },
        "dangling": [i for i in range(n) if colsum[i] == 0],
    }, open("skillpath_nodes.json", "w", encoding="utf-8"), ensure_ascii=False)

    # ---- validation summary ----
    print(f"course->skills : {len(course_skills)} courses")
    print(f"catalog courses w/ skills: {sum(1 for c in catalog if course_skills.get(c))}/{len(catalog)}")
    print(f"careers        : {len(careers)}")
    print(f"graph nodes    : {n} (courses+skills+careers) | edges: {A.nnz}")
    low = [l for l, c in careers.items() if c.get("low_sample")]
    print(f"low-sample careers (skills supplemented from related): {len(low)} {low}")
    print("sample careers:")
    for label in ["Data Analyst", "Software Engineer", "Accountant", "UX/UI Designer",
                  "Statistician", "Actuary", "Translator / Interpreter"]:
        c = careers[label]
        sal = c["salary"]
        top = ", ".join(s["name"] for s in c["skills"][:6])
        print(f"  {label}: skill_jobs={c['n_skill_jobs']} postings={c['n_postings']} "
              f"salary_median={sal['median']} top_skills=[{top}]")


if __name__ == "__main__":
    main()
