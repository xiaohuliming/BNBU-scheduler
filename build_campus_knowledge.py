"""Build the read-only campus retrieval SQLite index off the request path.

Run with the existing pandas/pypdf build environment and pdfplumber installed:
  python build_campus_knowledge.py --refresh-handbooks
Subsequent builds reuse /tmp/maxcourse-knowledge-sources. No credentials are used.
Only explicitly curated campus PDFs and public AR handbooks are ingested.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader

from campus_knowledge import INDEX_NAME, PROGRAMME_ZH, SCHEMA_VERSION, sha256_file, tokens
from maximize_credits import load_timetable, parse_session_from_title

HANDBOOK_URL = 'https://ar.bnbu.edu.cn/current_students/student_handbook/programme_handbook.htm'
COURSE_DESCRIPTION_URL = 'https://ar.bnbu.edu.cn/current_students/student_handbook/course_Deescription.htm'
TIMETABLE_URL = 'https://ecm.bnbu.edu.cn/index.html#doc/enterprise/121649'


def fetch_public(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'ar.bnbu.edu.cn' or parsed.username:
        raise ValueError('Only public Academic Registry HTTPS sources are allowed')
    # System curl uses macOS trust roots; do not disable TLS verification.
    safe = quote(url, safe=':/%?=&')
    return subprocess.check_output([
        'curl', '-fsS', '--proto', '=https', '--connect-timeout', '10',
        '--max-time', '40', '--retry', '2', '--max-filesize', '16777216', safe,
    ], stderr=subprocess.PIPE)


def refresh_handbooks(cache):
    cache.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(fetch_public(HANDBOOK_URL), 'html.parser')
    sources = []
    for link in soup.find_all('a', href=True):
        match = re.search(r'for (202[3-6]) Admission', link.get_text())
        if not match:
            continue
        cohort = match.group(1)
        landing = urljoin(HANDBOOK_URL, link['href'])
        page = BeautifulSoup(fetch_public(landing), 'html.parser')
        for doc in page.find_all('a', href=True):
            title = doc.get_text(' ', strip=True)
            url = urljoin(landing, doc['href'])
            if 'Programme' not in title or not urlsplit(url).path.lower().endswith('.pdf'):
                continue
            filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1])
            code = re.match(r'([A-Z]+)[ _-]', filename)
            if not code:
                raise ValueError('Unrecognized handbook programme: ' + filename)
            sources.append({'id': 'handbook:' + code.group(1) + ':' + cohort,
                            'title': title + ' · ' + cohort + '级培养手册',
                            'programme': code.group(1), 'cohort': cohort,
                            'source_url': quote(url, safe=':/%?=&'), 'landing_url': landing,
                            'source_file': filename})
    if len(sources) < 100 or len({x['id'] for x in sources}) != len(sources):
        raise ValueError('Incomplete or duplicated public handbook directory')

    def download(source):
        body = fetch_public(source['source_url'])
        if not body.startswith(b'%PDF-'):
            raise ValueError('Handbook is not a public PDF: ' + source['id'])
        name = source['id'].replace(':', '-') + '.pdf'
        (cache / name).write_bytes(body)
        return dict(source, cache_file=name, sha256=hashlib.sha256(body).hexdigest())

    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = list(pool.map(download, sources))
    manifest = {'retrieved_at': datetime.now(timezone.utc).isoformat(), 'sources': downloaded}
    (cache / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Downloaded public handbooks:', len(downloaded), flush=True)
    return manifest


def split_text(text, maximum=2200):
    lines = [line.rstrip() for line in text.replace('\x00', '').splitlines() if line.strip()]
    buffer = ''
    for line in lines:
        while len(line) > maximum:
            if buffer:
                yield buffer
                buffer = ''
            yield line[:maximum]
            line = line[maximum:]
        if buffer and len(buffer) + len(line) + 1 > maximum:
            yield buffer
            buffer = ''
        buffer += ('\n' if buffer else '') + line
    if buffer:
        yield buffer


def pdf_pages(path, handbook=False):
    if handbook:
        import pdfplumber
        from build_programmes import make_struck, strike_segments
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                struck = make_struck(strike_segments(page))
                filtered = page.filter(lambda obj: obj.get('object_type') != 'char' or not struck(obj))
                text = filtered.extract_text(layout=True) or ''
                if not text.strip():
                    if not any(page.objects.values()):
                        continue  # Verified empty trailing page; keep original page numbering.
                    raise ValueError(f'Handbook page needs OCR: {path.name}, page {number}')
                yield number, text
    else:
        for number, page in enumerate(PdfReader(path).pages, 1):
            text = page.extract_text() or ''
            if not text.strip():
                raise ValueError(f'Campus document page needs OCR: {path.name}, page {number}')
            yield number, text


def campus_documents(root, fingerprints):
    path = root / 'campus_docs.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    fingerprints[path.name] = sha256_file(path)
    for category in data['categories']:
        for entry in category['docs']:
            kind = entry.get('type')
            if kind not in ('pdf', 'location'):
                continue  # No installers, SSO content, or unrestricted link crawling.
            title = entry['title']
            source_url = entry['href']
            record = {'id': 'campus:' + hashlib.sha256((source_url + '\n' + title).encode()).hexdigest()[:16],
                      'title': title, 'kind': 'office' if kind == 'location' else 'campus',
                      'programmes': [p for p in entry.get('keywords', []) if p in PROGRAMME_ZH],
                      'keywords': ' '.join(entry.get('keywords', [])) + ' ' + category['title'],
                      'source_url': source_url, 'source_label': title,
                      'directory_updated': data['updated'], 'source_version': entry.get('meta', ''),
                      'cohort': '', 'semester': '',
                      'notice': '文件适用期见 source_version 和原文；历史申请时间不能当作本学期安排。',
                      'source_type': 'curated_directory' if kind == 'location' else 'official_pdf',
                      'chunks': []}
            if kind == 'location':
                record['chunks'] = [(None, title + '\n' + entry.get('desc', '') + '\n' + entry.get('meta', ''))]
                record['notice'] = '人工整理的联系地点，请核对来源。'
            else:
                source = root / urlsplit(source_url).path.lstrip('/')
                if not source.resolve().is_relative_to((root / 'docs').resolve()):
                    raise ValueError('Unexpected campus PDF path')
                fingerprints[str(source.relative_to(root))] = sha256_file(source)
                record['sha256'] = fingerprints[str(source.relative_to(root))]
                for page, text in pdf_pages(source):
                    record['chunks'].extend((page, chunk) for chunk in split_text(text))
            yield record


def handbook_documents(cache, manifest):
    for source in sorted(manifest['sources'], key=lambda x: x['id']):
        path = cache / source['cache_file']
        if sha256_file(path) != source['sha256']:
            raise ValueError('Handbook cache checksum mismatch')
        record = {k: v for k, v in source.items() if k not in ('cache_file', 'programme')}
        record.update(kind='handbook', programmes=[source['programme']], semester='',
                      keywords=PROGRAMME_ZH.get(source['programme'], '') + ' 培养方案 毕业要求 programme handbook study plan',
                      source_type='official_pdf', retrieved_at=manifest['retrieved_at'],
                      notice='仅适用于标注的入学年份。已按版面移除删除线修订，表格要求请结合原始 PDF 核对。', chunks=[])
        text_cache = path.with_suffix('.text.json')
        cached = json.loads(text_cache.read_text(encoding='utf-8')) if text_cache.exists() else {}
        if cached.get('sha256') == source['sha256'] and cached.get('extractor') == 'layout-strike-v1':
            record['chunks'] = cached['chunks']
        else:
            for page, text in pdf_pages(path, handbook=True):
                record['chunks'].extend((page, chunk) for chunk in split_text(text))
            text_cache.write_text(json.dumps({'sha256': source['sha256'], 'extractor': 'layout-strike-v1', 'chunks': record['chunks']}, ensure_ascii=False), encoding='utf-8')
        if not record['chunks']:
            raise ValueError('Handbook has no readable text: ' + source['id'])
        yield record


def course_documents(root, fingerprints):
    paths = sorted(p for p in root.glob('*.xlsx') if 'Course List' in p.name and not p.name.startswith('~$'))
    if len(paths) != 1:
        raise ValueError('Expected exactly one current Course List workbook')
    path = paths[0]
    match = re.search(r'Semester\s*(\d)\s*of\s*AY(\d{4})-(\d{2})', path.name)
    if not match:
        raise ValueError('Cannot identify timetable semester')
    semester = match.group(2)[2:] + match.group(3) + 'S' + match.group(1)
    fingerprints[path.name] = sha256_file(path)
    catalogue_path = root / 'course_catalog.json'
    fingerprints[catalogue_path.name] = sha256_file(catalogue_path)
    catalogue = json.loads(catalogue_path.read_text(encoding='utf-8'))
    df = load_timetable(str(path))
    for code, group in df.groupby('Course Code', sort=True):
        title_session = str(group['Course Title & Session'].iloc[0])
        title = re.sub(r'\s*\(\d+\)\s*$', '', title_session).strip()
        programmes = sorted({str(x) for x in group['Offering Programme'].dropna()})
        record = {'id': 'course:' + semester + ':' + str(code), 'title': str(code) + ' ' + title,
                  'kind': 'course', 'cohort': '', 'semester': semester, 'programmes': programmes,
                  'keywords': '本学期 课表 教师 教室 先修 timetable class schedule course ' + ' '.join(PROGRAMME_ZH.get(p, '') for p in programmes),
                  'source_url': TIMETABLE_URL, 'description_source_url': COURSE_DESCRIPTION_URL,
                  'source_label': path.name, 'source_requires_school_login': True,
                  'snapshot_date': re.search(r'(20\d{6})', path.stem).group(1),
                  'source_file': path.name, 'sha256': fingerprints[path.name],
                  'source_type': 'official_timetable_and_catalogue', 'course_code': str(code),
                  'meeting_rows': len(group), 'chunks': [],
                  'notice': '课程安排来自文件所标注的课表快照，不是 MIS 实时余位或选课结果。每个班级可有多条教学安排。'}
        info = catalogue.get(str(code), {})
        introduction = record['title'] + '\n学期 Semester: ' + semester + '\n'
        introduction += '开课专业 Offering Programme: ' + ', '.join(programmes) + '\n'
        if info.get('description'):
            introduction += 'Course description: ' + info['description'] + '\n'
        record['chunks'].extend((None, chunk) for chunk in split_text(introduction))
        # Keep every spreadsheet row and session; never deduplicate moving rooms.
        for _, row in group.iterrows():
            values = row.where(row.notna(), '').to_dict()
            session = parse_session_from_title(values['Course Title & Session']) or ''
            lines = [record['title'], '学期 Semester: ' + semester, '班级 Session: ' + session]
            for field, label in [('Units', '学分 Units'), ('Offering Programme', '开课专业'),
                                 ('Curriculum Type', '课程类别'), ('Elective Type', '选修类别'),
                                 ('Teachers', '教师 Teachers'), ('Class Schedule', '上课时间'),
                                 ('Hours', '课时 Hours'), ('Classroom', '教室 Classroom'),
                                 ('Requirements', '修读要求 Requirements'), ('Remarks', '备注 Remarks')]:
                if str(values.get(field, '')):
                    lines.append(label + ': ' + str(values[field]))
            record['chunks'].extend((None, chunk) for chunk in split_text('\n'.join(lines)))
        yield record


def write_index(path, records, summary):
    temporary = path.with_suffix('.building.sqlite')
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    try:
        conn.executescript('''
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                cohort TEXT NOT NULL, semester TEXT NOT NULL, programmes TEXT NOT NULL, metadata TEXT NOT NULL);
            CREATE TABLE chunks (document_id TEXT NOT NULL, ordinal INTEGER NOT NULL, page INTEGER, text TEXT NOT NULL);
            CREATE INDEX chunks_document ON chunks(document_id, ordinal);
            CREATE INDEX documents_kind ON documents(kind, cohort, semester);
            CREATE VIRTUAL TABLE chunk_fts USING fts5(title, body, content='', tokenize='unicode61');
        ''')
        for record in records:
            metadata = {k: v for k, v in record.items() if k not in ('chunks', 'keywords')}
            conn.execute('INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)', (
                record['id'], record['title'], record['kind'], record.get('cohort', ''),
                record.get('semester', ''), '|' + '|'.join(record.get('programmes', [])) + '|',
                json.dumps(metadata, ensure_ascii=False)))
            title_tokens = ' '.join(tokens(record['title'] + ' ' + record.get('keywords', '') + ' ' + ' '.join(record.get('programmes', []))))
            for ordinal, (page, text) in enumerate(record['chunks']):
                cursor = conn.execute('INSERT INTO chunks VALUES (?, ?, ?, ?)', (record['id'], ordinal, page, text))
                conn.execute('INSERT INTO chunk_fts(rowid, title, body) VALUES (?, ?, ?)',
                             (cursor.lastrowid, title_tokens, ' '.join(tokens(text))))
        summary.update(schema_version=SCHEMA_VERSION, documents=len(records),
                       chunks=sum(len(d['chunks']) for d in records),
                       counts=dict(Counter(d['kind'] for d in records)),
                       cohorts=sorted({d['cohort'] for d in records if d.get('cohort')}),
                       semesters=sorted({d['semester'] for d in records if d.get('semester')}),
                       programmes=sorted({p for d in records for p in d.get('programmes', [])}),
                       meeting_rows=sum(d.get('meeting_rows', 0) for d in records))
        conn.execute('INSERT INTO metadata VALUES (?, ?)', ('summary', json.dumps(summary, ensure_ascii=False)))
        conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute('VACUUM')
    finally:
        conn.close()
    os.replace(temporary, path)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-handbooks', action='store_true')
    parser.add_argument('--cache', type=Path, default=Path('/tmp/maxcourse-knowledge-sources'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest_path = args.cache / 'manifest.json'
    if args.refresh_handbooks or not manifest_path.exists():
        manifest = refresh_handbooks(args.cache)
    else:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    fingerprints = {}
    documents = list(campus_documents(root, fingerprints))
    print('Campus documents:', len(documents), flush=True)
    for record in handbook_documents(args.cache, manifest):
        documents.append(record)
        if len(documents) % 20 == 0:
            print('Extracted:', record['id'], flush=True)
    documents.extend(course_documents(root, fingerprints))
    summary = write_index(root / INDEX_NAME, documents, {
        'built_at': datetime.now(timezone.utc).isoformat(), 'local_sources': fingerprints,
        'handbook_source': HANDBOOK_URL, 'handbook_retrieved_at': manifest['retrieved_at'],
        'retrieval': 'BM25 full-text with Chinese bigrams and bilingual query expansion',
    })
    print(json.dumps({k: v for k, v in summary.items() if k not in ('local_sources', 'programmes')}, ensure_ascii=False, indent=2))
    print('Index bytes:', (root / INDEX_NAME).stat().st_size)


if __name__ == '__main__':
    main()
