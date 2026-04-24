import os
import glob
import json
import re
import sqlite3
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from maximize_credits import load_timetable, maximize_credits, fmt_meeting, parse_schedule
from crawler import fetch_timeline

# Database setup
DB_PATH = 'maxcourse.db'
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
SECRET_KEY_FILE = os.path.join(APP_ROOT, '.flask_secret_key')
SESSION_LIFETIME_DAYS = 36500
DAY_SEQUENCE = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_MAP = {day: index for index, day in enumerate(DAY_SEQUENCE)}
DAY_LABELS = {
    "Mon": "周一",
    "Tue": "周二",
    "Wed": "周三",
    "Thu": "周四",
    "Fri": "周五",
    "Sat": "周六",
    "Sun": "周日",
}
SCHOOL_DAY_END_MINUTES = 21 * 60 + 50
EXCLUDED_FREE_CLASSROOM_BUILDINGS = {'V22', 'V20', 'UC', 'SP'}
PRIORITY_BUILDING_ORDER = ['T8', 'T7', 'T6', 'T5', 'T4', 'T29']


def load_or_create_secret_key():
    env_key = os.getenv('MAXCOURSE_SECRET_KEY')
    if env_key:
        return env_key

    try:
        with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as file:
            secret_key = file.read().strip()
            if secret_key:
                return secret_key
    except FileNotFoundError:
        pass

    secret_key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, 'w', encoding='utf-8') as file:
        file.write(secret_key)

    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass

    return secret_key


app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = load_or_create_secret_key()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_REFRESH_EACH_REQUEST=True,
)


@app.errorhandler(Exception)
def handle_api_errors(error):
    if not request.path.startswith('/api/'):
        return error

    if isinstance(error, HTTPException):
        return jsonify({
            "error": error.description,
            "status": error.code,
            "path": request.path,
        }), error.code

    app.logger.exception("Unhandled API error")
    return jsonify({"error": "Internal server error"}), 500


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                ispace_username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ispace_id INTEGER,
                title TEXT,
                course TEXT,
                due_date INTEGER,
                url TEXT,
                description TEXT,
                is_completed BOOLEAN DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS teacher_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_name TEXT,
                user_id INTEGER,
                rating INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_id TEXT,
                user_id INTEGER,
                view_name TEXT,
                path TEXT,
                referrer TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        # Check if description column exists, add if not (migration)
        try:
            c.execute('ALTER TABLE todos ADD COLUMN description TEXT')
        except sqlite3.OperationalError:
            pass # Column already exists
            
        # Migration for teacher_ratings
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN comment TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN is_anonymous BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE teacher_ratings ADD COLUMN course_info TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
        except sqlite3.OperationalError:
            pass

        try:
            c.execute('ALTER TABLE todos ADD COLUMN is_stale BOOLEAN DEFAULT 0')
        except sqlite3.OperationalError:
            pass

        c.execute('CREATE INDEX IF NOT EXISTS idx_todos_user_ispace_lookup ON todos (user_id, ispace_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views (created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_view_name ON page_views (view_name)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_page_views_visitor_id ON page_views (visitor_id)')
        try:
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_user_ispace_unique ON todos (user_id, ispace_id) WHERE ispace_id IS NOT NULL')
        except sqlite3.IntegrityError:
            pass
            
        conn.commit()

init_db()

# Global cache for the dataframe
df_cache = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def set_authenticated_session(user_id, username, display_name):
    session.permanent = True
    session['user_id'] = user_id
    session['username'] = username
    session['display_name'] = display_name


def sync_ispace_todos_for_user(conn, user_id, items):
    c = conn.cursor()
    added = 0
    updated = 0
    seen_ids = []

    for item in items:
        ispace_id = item.get('id')
        if ispace_id is None:
            continue

        seen_ids.append(ispace_id)
        title = item.get('name')
        course = item.get('course')
        due_date = item.get('due_date')
        url = item.get('url')

        c.execute(
            '''
            SELECT id, title, course, due_date, url, COALESCE(is_stale, 0) AS is_stale
            FROM todos
            WHERE user_id = ? AND ispace_id = ?
            ORDER BY id ASC
            LIMIT 1
            ''',
            (user_id, ispace_id),
        )
        existing = c.fetchone()

        if existing:
            c.execute(
                '''
                UPDATE todos
                SET title = ?, course = ?, due_date = ?, url = ?, is_stale = 0
                WHERE user_id = ? AND ispace_id = ?
                ''',
                (title, course, due_date, url, user_id, ispace_id),
            )
            if (
                existing['title'] != title
                or existing['course'] != course
                or existing['due_date'] != due_date
                or existing['url'] != url
                or existing['is_stale']
            ):
                updated += 1
        else:
            c.execute(
                '''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url, is_stale)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ''',
                (user_id, ispace_id, title, course, due_date, url),
            )
            added += 1

    if seen_ids:
        placeholders = ','.join('?' for _ in seen_ids)
        c.execute(
            f'''
            UPDATE todos
            SET is_stale = 1
            WHERE user_id = ?
              AND ispace_id IS NOT NULL
              AND ispace_id NOT IN ({placeholders})
              AND COALESCE(is_stale, 0) = 0
            ''',
            [user_id, *seen_ids],
        )
    else:
        c.execute(
            '''
            UPDATE todos
            SET is_stale = 1
            WHERE user_id = ?
              AND ispace_id IS NOT NULL
              AND COALESCE(is_stale, 0) = 0
            ''',
            (user_id,),
        )

    return {
        "added": added,
        "updated": updated,
        "stale": c.rowcount,
    }

def get_excel_file():
    files = glob.glob("*.xlsx") + glob.glob("*.xls")
    files = [f for f in files if not f.startswith("~$")]
    if not files:
        return None
    for f in files:
        if "Course List" in f:
            return f
    return files[0] if files else None

def get_df():
    global df_cache
    if df_cache is not None:
        return df_cache
    
    file_path = get_excel_file()
    if not file_path:
        raise FileNotFoundError("No Excel file found in directory")
    
    print(f"Loading data from {file_path}...")
    df_cache = load_timetable(file_path)
    return df_cache


def time_to_minutes(value):
    hours, minutes = str(value).strip().split(':')
    return int(hours) * 60 + int(minutes)


def minutes_to_time(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def extract_building(room):
    room = str(room).strip()
    if '-' not in room:
        return room.upper()
    return room.split('-', 1)[0].strip().upper()


def building_sort_key(building):
    building = str(building or '').strip().upper()

    if building in PRIORITY_BUILDING_ORDER:
        return (0, PRIORITY_BUILDING_ORDER.index(building), 0, '', 0, building)

    if building == 'CC':
        return (2, 0, 0, '', 0, building)

    match = re.match(r'^([A-Z]+)(\d+)$', building)
    if match:
        prefix, number = match.groups()
        return (1, 0, 0, prefix, int(number), building)

    return (1, 1, 1, building, 0, building)


def is_room_like(room):
    room = str(room).strip()
    return bool(room) and room.lower() != 'nil' and '-' in room and ' ' not in room


def normalize_room_tokens(raw_room):
    seen = set()
    rooms = []
    for part in str(raw_room or '').split('/'):
        room = part.strip()
        if is_room_like(room) and room not in seen:
            seen.add(room)
            rooms.append(room)
    return rooms


def serialize_room_event(event):
    return {
        "course_code": event["course_code"],
        "title": event["title"],
        "teacher": event["teacher"],
        "start": minutes_to_time(event["start_min"]),
        "end": minutes_to_time(event["end_min"]),
    }


def build_classroom_index():
    df = get_df()
    room_index = {}
    room_entries = {}

    for _, row in df.iterrows():
        meeting = parse_schedule(str(row.get('Class Schedule', '')).strip())
        if meeting is None:
            continue

        rooms = normalize_room_tokens(row.get('Classroom', ''))
        if not rooms:
            continue

        day_index, start_min, end_min = meeting
        event = {
            "day_index": day_index,
            "start_min": start_min,
            "end_min": end_min,
            "course_code": str(row.get('Course Code', '')).strip(),
            "title": str(row.get('Course Title & Session', '')).strip(),
            "teacher": str(row.get('Teachers', '')).strip(),
        }

        for room in rooms:
            room_entries.setdefault(room, []).append(event)
            room_index.setdefault(room, {"room": room, "building": extract_building(room)})

    for room, events in room_entries.items():
        events.sort(key=lambda item: (item["day_index"], item["start_min"], item["end_min"], item["course_code"]))

    rooms = [
        room_index[key]
        for key in sorted(
            room_index.keys(),
            key=lambda room: (building_sort_key(extract_building(room)), room)
        )
        if extract_building(key) not in EXCLUDED_FREE_CLASSROOM_BUILDINGS
    ]
    return rooms, room_entries

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/ddl')
def ddl_page():
    return send_from_directory('.', 'ddl.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('.', 'favicon.png', mimetype='image/png')


@app.before_request
def refresh_logged_in_session():
    if 'user_id' in session:
        session.permanent = True


def get_analytics_visitor_id():
    if 'analytics_visitor_id' not in session:
        session['analytics_visitor_id'] = uuid.uuid4().hex
        session.permanent = True
    return session['analytics_visitor_id']

# --- Auth Endpoints ---

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
        
    conn = get_db()
    try:
        password_hash = generate_password_hash(password)
        c = conn.cursor()
        c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
        conn.commit()
        return jsonify({"success": True, "message": "Registered successfully"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 400
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    conn.close()
    
    stored_password_hash = user['password_hash'] if user else None

    if user and stored_password_hash and check_password_hash(stored_password_hash, password):
        display_name = user['display_name'] if user['display_name'] else user['ispace_username'] or user['username']
        set_authenticated_session(user['id'], user['username'], display_name)
        return jsonify({"success": True, "user": {"id": user['id'], "username": user['username'], "ispace_username": user['ispace_username'], "display_name": session['display_name']}})
    
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/login/ispace', methods=['POST'])
def login_ispace():
    data = request.json
    username = data.get('username') # Student ID
    password = data.get('password')
    
    # 1. Verify with iSpace
    result = fetch_timeline(username, password)
    if isinstance(result, dict) and "error" in result:
        return jsonify({"error": "iSpace login failed: " + result["error"]}), 401
        
    # 2. Login successful, get DDLs
    ddls = result
    
    # 3. Create or Update local user
    conn = get_db()
    c = conn.cursor()
    
    # Check if user exists by ispace_username (or just username if they registered with student ID)
    # Strategy: We treat iSpace login as a way to "bind" or "quick login".
    # If a user with this username exists, we log them in. If not, we create a shadow user.
    
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    
    user_id = None
    display_name = username # Default display name is username (Student ID)
    
    if user:
        user_id = user['id']
        # Update ispace_username if not set
        if not user['ispace_username']:
            c.execute('UPDATE users SET ispace_username = ? WHERE id = ?', (username, user_id))
        
        # Use existing display name if set, otherwise use ispace username
        if user['display_name']:
            display_name = user['display_name']
        else:
            # If no display name, default to ispace username (Student ID)
            # We can try to fetch real name from ispace if possible, but for now use ID
            pass
            
    else:
        # Create new user
        # We don't have a local password for them, so we set a dummy hash or handle it.
        # For simplicity, we create a user with username=studentID and no password (so they can only login via iSpace)
        # or we ask them to set a password later.
        # Set display_name to username initially
        c.execute('INSERT INTO users (username, ispace_username, display_name) VALUES (?, ?, ?)', (username, username, username))
        user_id = c.lastrowid
        
    # 4. Sync DDLs to Todos
    sync_stats = sync_ispace_todos_for_user(conn, user_id, ddls)

    conn.commit()
    conn.close()
    
    set_authenticated_session(user_id, username, display_name)
    
    return jsonify({
        "success": True,
        "user": {"id": user_id, "username": username, "ispace_username": username, "display_name": display_name},
        "sync": sync_stats,
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route('/api/user', methods=['GET'])
def get_current_user():
    if 'user_id' not in session:
        return jsonify({"user": None})
    
    # Refresh user info from DB
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    user = c.fetchone()
    conn.close()
    
    if not user:
        return jsonify({"user": None})
        
    display_name = user['display_name'] if user['display_name'] else user['ispace_username'] or user['username']
    session['display_name'] = display_name # Sync session
    
    return jsonify({"user": {"id": user['id'], "username": user['username'], "ispace_username": user['ispace_username'], "display_name": display_name}})

@app.route('/api/user/profile', methods=['PUT'])
def update_profile():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    display_name = data.get('display_name')
    
    if not display_name:
        return jsonify({"error": "Display name required"}), 400
        
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('UPDATE users SET display_name = ? WHERE id = ?', (display_name, session['user_id']))
        conn.commit()
        session['display_name'] = display_name
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/teachers', methods=['GET'])
def get_all_teachers():
    try:
        df = get_df()
        # Extract unique teachers and calculate basic stats
        # We need a list of teachers with: name, course_count, avg_rating
        
        # 1. Get all teachers from dataframe
        # Explode the 'Teachers' column if it contains multiple teachers? 
        # The current logic assumes 'Teachers' column is a string. 
        # Let's clean and split if necessary, but current get_courses just lists them.
        # Let's iterate unique values in 'Teachers' column.
        
        all_teachers = set()
        teacher_course_counts = {}
        
        # Safe iteration
        if 'Teachers' in df.columns:
            for teachers_str in df['Teachers'].dropna().astype(str):
                # Split by comma and newline to handle multiple teachers
                parts = teachers_str.replace('\n', ',').split(',')
                for part in parts:
                    t_name = part.strip()
                    if t_name and t_name.lower() != 'nan':
                        all_teachers.add(t_name)
                        teacher_course_counts[t_name] = teacher_course_counts.get(t_name, 0) + 1

        teacher_list = []
        conn = get_db()
        c = conn.cursor()
        
        for name in all_teachers:
            # Get average rating
            c.execute('SELECT rating FROM teacher_ratings WHERE teacher_name = ?', (name,))
            ratings = [r[0] for r in c.fetchall()]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0
            rating_count = len(ratings)
            
            teacher_list.append({
                "name": name,
                "course_count": teacher_course_counts.get(name, 0),
                "average_rating": avg_rating,
                "rating_count": rating_count
            })
            
        conn.close()
        
        # Sort by rating count (popular) then name
        teacher_list.sort(key=lambda x: (-x['rating_count'], x['name']))
        
        return jsonify(teacher_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/user/delete', methods=['DELETE'])
def delete_user_data():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session['user_id']
    conn = get_db()
    c = conn.cursor()
    
    try:
        # 1. Delete all todos
        c.execute('DELETE FROM todos WHERE user_id = ?', (user_id,))
        
        # 2. Unlink teacher ratings (set user_id to NULL to preserve rating but remove link)
        # Note: We need to check if schema allows NULL.
        # Schema: FOREIGN KEY(user_id) REFERENCES users(id)
        # It doesn't explicitly say NOT NULL, so it should allow NULL.
        c.execute('UPDATE teacher_ratings SET user_id = NULL, is_anonymous = 1 WHERE user_id = ?', (user_id,))
        
        # 3. Delete user
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        
        conn.commit()
        session.clear()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/teachers/<path:name>', methods=['GET'])
def get_teacher_profile(name):
    try:
        df = get_df()
        # Find courses where 'Teachers' column contains the name or equals it
        # For simplicity and performance, we'll iterate through cached data structure if possible, 
        # but here we use pandas.
        # Note: The name comes from the frontend which took it from 'Teachers' column.
        # We will look for rows where the 'Teachers' column value matches exactly what was sent,
        # OR contains it if we want to be more flexible. Given the frontend logic, exact match on the cell value is safest
        # to replicate the 'grouping' seen in ExplorerView, but ideally we want "All courses by Dr. X".
        # Let's do a contains search to be more helpful.
        
        # Safe string search handling NaN
        teacher_courses_df = df[df['Teachers'].astype(str).str.contains(name, regex=False, case=False, na=False)]
        
        courses_list = []
        if not teacher_courses_df.empty:
            for _, row in teacher_courses_df.iterrows():
                row_data = row.where(pd.notnull(row), "").to_dict()
                # Clean up keys for frontend
                clean_row = {
                    "code": row_data.get('Course Code', ''),
                    "title": row_data.get('Course Title & Session', ''),
                    "units": row_data.get('Units', ''),
                    "schedule": row_data.get('Class Schedule', ''),
                    "classroom": row_data.get('Classroom', ''),
                    "teachers": row_data.get('Teachers', '')
                }
                courses_list.append(clean_row)
        
        # Get ratings
        conn = get_db()
        c = conn.cursor()
        
        # Get ratings and comments
        c.execute('SELECT rating, comment, is_anonymous, user_id, created_at, course_info FROM teacher_ratings WHERE teacher_name = ? ORDER BY created_at DESC', (name,))
        rows = c.fetchall()
        
        ratings = [r['rating'] for r in rows]
        comments = []
        
        for r in rows:
            if r['comment']:
                username = "Anonymous"
                if not r['is_anonymous']:
                    # Fetch username and display_name
                    c2 = conn.cursor()
                    c2.execute('SELECT username, display_name FROM users WHERE id = ?', (r['user_id'],))
                    u = c2.fetchone()
                    if u:
                        username = u['display_name'] if u['display_name'] else u['username']
                
                comments.append({
                    "rating": r['rating'],
                    "comment": r['comment'],
                    "user": username,
                    "date": r['created_at'],
                    "course": r['course_info']
                })
        
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
        
        # Check if current user has rated
        user_rating = None
        user_comment = ""
        user_is_anonymous = False
        user_course = ""
        
        if 'user_id' in session:
            c.execute('SELECT rating, comment, is_anonymous, course_info FROM teacher_ratings WHERE teacher_name = ? AND user_id = ?', (name, session['user_id']))
            row = c.fetchone()
            if row:
                user_rating = row['rating']
                user_comment = row['comment']
                user_is_anonymous = bool(row['is_anonymous'])
                user_course = row['course_info']
        
        conn.close()

        return jsonify({
            "name": name,
            "courses": courses_list,
            "average_rating": avg_rating,
            "rating_count": len(ratings),
            "user_rating": user_rating,
            "user_comment": user_comment,
            "user_is_anonymous": user_is_anonymous,
            "user_course": user_course,
            "comments": comments
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/teachers/<path:name>/rate', methods=['POST'])
def rate_teacher(name):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    rating = data.get('rating')
    comment = data.get('comment', '')
    is_anonymous = data.get('is_anonymous', False)
    course_info = data.get('course', '')
    
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        return jsonify({"error": "Invalid rating (1-5)"}), 400
        
    conn = get_db()
    c = conn.cursor()
    
    # Upsert rating
    c.execute('SELECT id FROM teacher_ratings WHERE teacher_name = ? AND user_id = ?', (name, session['user_id']))
    exists = c.fetchone()
    
    if exists:
        c.execute('UPDATE teacher_ratings SET rating = ?, comment = ?, is_anonymous = ?, course_info = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?', (rating, comment, is_anonymous, course_info, exists[0]))
    else:
        c.execute('INSERT INTO teacher_ratings (teacher_name, user_id, rating, comment, is_anonymous, course_info) VALUES (?, ?, ?, ?, ?, ?)', (name, session['user_id'], rating, comment, is_anonymous, course_info))
        
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})

# --- Todo Endpoints ---

@app.route('/api/todos', methods=['GET'])
def get_todos():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM todos WHERE user_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY due_date ASC, id ASC', (session['user_id'],))
    todos = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify(todos)

@app.route('/api/todos/sync', methods=['POST'])
def sync_todos():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    ispace_user = data.get('username')
    ispace_pass = data.get('password')
    
    if not ispace_user or not ispace_pass:
         return jsonify({"error": "Credentials required"}), 400

    user_id = session['user_id']
    submitted_ispace_user = str(ispace_user).strip()

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT ispace_username FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    conn.close()

    linked_ispace_user = str(user['ispace_username']).strip() if user and user['ispace_username'] else ''
    if not linked_ispace_user or linked_ispace_user != submitted_ispace_user:
        return jsonify({
            "error": "iSpace account mismatch. Please log in with the matching iSpace account before syncing."
        }), 403

    result = fetch_timeline(ispace_user, ispace_pass)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
        
    conn = get_db()
    c = conn.cursor()
    
    sync_stats = sync_ispace_todos_for_user(conn, user_id, result)

    conn.commit()
    conn.close()
    
    return jsonify({"success": True, **sync_stats})

@app.route('/api/todos/add', methods=['POST'])
def add_todo():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    title = data.get('title')
    course = data.get('course')
    description = data.get('description')
    due_date = data.get('due_date') # Unix timestamp or ISO string
    
    if not title:
        return jsonify({"error": "Title required"}), 400
        
    # Convert ISO date string to timestamp if necessary
    if isinstance(due_date, str):
        try:
            dt = datetime.fromisoformat(due_date.replace('Z', '+00:00'))
            due_date = int(dt.timestamp())
        except ValueError:
            pass # Assume it's already int or handle error
            
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO todos (user_id, title, course, description, due_date, is_completed)
        VALUES (?, ?, ?, ?, ?, 0)
    ''', (session['user_id'], title, course, description, due_date))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    
    return jsonify({"success": True, "id": new_id})

@app.route('/api/todos/<int:todo_id>', methods=['PUT'])
def update_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    description = data.get('description')
    
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET description = ? WHERE id = ? AND user_id = ?', (description, todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>/complete', methods=['POST'])
def complete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET is_completed = 1 WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>/incomplete', methods=['POST'])
def incomplete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE todos SET is_completed = 0 WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/todos/<int:todo_id>', methods=['DELETE'])
def delete_todo(todo_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM todos WHERE id = ? AND user_id = ?', (todo_id, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --- Existing API Endpoints ---

@app.route('/api/ddl', methods=['POST'])
def get_ddl():
    # Keep this for backward compatibility or direct checking
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
        
    result = fetch_timeline(username, password)
    
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
        
    return jsonify(result)


@app.route('/api/analytics/track', methods=['POST'])
def track_page_view():
    data = request.get_json(silent=True) or {}
    view_name = str(data.get('view') or 'unknown').strip()[:80]
    path = str(data.get('path') or request.referrer or '').strip()[:300]
    referrer = str(data.get('referrer') or request.referrer or '').strip()[:300]
    user_agent = str(request.headers.get('User-Agent') or '').strip()[:300]
    visitor_id = get_analytics_visitor_id()
    user_id = session.get('user_id')

    conn = get_db()
    c = conn.cursor()
    c.execute(
        '''
        INSERT INTO page_views (visitor_id, user_id, view_name, path, referrer, user_agent)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (visitor_id, user_id, view_name, path, referrer, user_agent),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route('/api/analytics/summary', methods=['GET'])
def get_analytics_summary():
    conn = get_db()
    c = conn.cursor()

    total_views = c.execute('SELECT COUNT(*) FROM page_views').fetchone()[0]
    unique_visitors = c.execute('SELECT COUNT(DISTINCT visitor_id) FROM page_views').fetchone()[0]
    today_views = c.execute(
        "SELECT COUNT(*) FROM page_views WHERE date(created_at) = date('now')"
    ).fetchone()[0]

    c.execute(
        '''
        SELECT view_name, COUNT(*) AS views, COUNT(DISTINCT visitor_id) AS visitors
        FROM page_views
        GROUP BY view_name
        ORDER BY views DESC, view_name ASC
        '''
    )
    by_view = [dict(row) for row in c.fetchall()]
    conn.close()

    return jsonify({
        "totalViews": total_views,
        "uniqueVisitors": unique_visitors,
        "todayViews": today_views,
        "byView": by_view,
    })


@app.route('/api/free-classrooms', methods=['GET'])
def get_free_classrooms():
    day = str(request.args.get('day', 'Mon')).strip()
    start = str(request.args.get('start', '08:00')).strip()
    end = str(request.args.get('end', '08:50')).strip()

    if day not in DAY_MAP:
        return jsonify({"error": "Invalid day"}), 400

    try:
        start_min = time_to_minutes(start)
        end_min = time_to_minutes(end)
    except Exception:
        return jsonify({"error": "Invalid time format, expected HH:MM"}), 400

    if end_min <= start_min:
        return jsonify({"error": "End time must be later than start time"}), 400

    rooms, room_entries = build_classroom_index()
    day_index = DAY_MAP[day]

    free_rooms = []
    building_totals = Counter()
    free_buildings = Counter()

    for room_info in rooms:
        room = room_info["room"]
        building = room_info["building"]
        building_totals[building] += 1

        entries = [entry for entry in room_entries.get(room, []) if entry["day_index"] == day_index]
        has_conflict = any(start_min < entry["end_min"] and entry["start_min"] < end_min for entry in entries)
        if has_conflict:
            continue

        previous_busy = None
        next_busy = None

        for entry in entries:
            if entry["end_min"] <= start_min:
                if previous_busy is None or entry["end_min"] > previous_busy["end_min"]:
                    previous_busy = entry
            if entry["start_min"] >= end_min:
                if next_busy is None or entry["start_min"] < next_busy["start_min"]:
                    next_busy = entry

        free_buildings[building] += 1
        free_rooms.append({
            "room": room,
            "building": building,
            "free_until": minutes_to_time(next_busy["start_min"]) if next_busy else minutes_to_time(SCHOOL_DAY_END_MINUTES),
            "previous_busy": serialize_room_event(previous_busy) if previous_busy else None,
            "next_busy": serialize_room_event(next_busy) if next_busy else None,
        })

    free_rooms.sort(key=lambda item: (building_sort_key(item["building"]), item["room"]))
    total_rooms = len(rooms)

    building_summary = []
    for building in sorted(building_totals.keys(), key=building_sort_key):
        total = building_totals[building]
        free = free_buildings.get(building, 0)
        building_summary.append({
            "building": building,
            "total_rooms": total,
            "free_rooms": free,
            "occupied_rooms": total - free,
        })

    return jsonify({
        "query": {
            "day": day,
            "day_label": DAY_LABELS.get(day, day),
            "start": minutes_to_time(start_min),
            "end": minutes_to_time(end_min),
        },
        "summary": {
            "total_rooms": total_rooms,
            "free_rooms": len(free_rooms),
            "occupied_rooms": total_rooms - len(free_rooms),
        },
        "buildings": building_summary,
        "rooms": free_rooms,
    })

@app.route('/api/courses', methods=['GET'])
def get_courses():
    try:
        df = get_df()
        courses = []
        grouped = df.groupby('Course Code')
        
        for code, group in grouped:
            title_full = str(group['Course Title & Session'].iloc[0])
            title = title_full.split('(')[0].strip()
            teachers = group['Teachers'].unique().tolist()
            teachers = [str(t) for t in teachers if pd.notna(t)]
            
            details = []
            for _, row in group.iterrows():
                row_data = row.where(pd.notnull(row), "").to_dict()
                details.append(row_data)

            courses.append({
                "code": code,
                "name": title,
                "teachers": teachers,
                "details": details
            })
            
        return jsonify(courses)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/optimize', methods=['POST'])
def optimize():
    try:
        data = request.json
        target_codes = data.get('codes', [])
        start_time_str = data.get('startTime')
        end_time_str = data.get('endTime')
        
        time_range = None
        if start_time_str and end_time_str:
            try:
                def parse_min(t):
                    h, m = map(int, t.split(':'))
                    return h * 60 + m
                min_t = parse_min(start_time_str)
                max_t = parse_min(end_time_str)
                time_range = (min_t, max_t)
            except Exception:
                pass

        blocked_raw = data.get('blocked', [])
        blocked_slots = []
        DAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
        
        for b in blocked_raw:
            try:
                day_str = b.get('day')
                s_str = b.get('start')
                e_str = b.get('end')
                
                if day_str in DAY_MAP and s_str and e_str:
                    day_idx = DAY_MAP[day_str]
                    def parse_min(t):
                        h, m = map(int, t.split(':'))
                        return h * 60 + m
                    s_min = parse_min(s_str)
                    e_min = parse_min(e_str)
                    blocked_slots.append((day_idx, s_min, e_min))
            except Exception:
                continue

        if not target_codes:
            return jsonify({"error": "No course codes provided"}), 400
            
        teacher_constraints = data.get('teachers', {})

        df = get_df()
        result = maximize_credits(
            df, 
            target_codes, 
            time_range=time_range, 
            blocked_slots=blocked_slots,
            teacher_constraints=teacher_constraints
        )
        
        if not result['solutions']:
            return jsonify({
                "found": False,
                "best_units": result['best_units'],
                "missing": result.get("missing", [])
            })

        sol = result['solutions'][0]
        
        # Format for frontend
        REV_DAY = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
        
        formatted_courses = []
        for c in sol:
            schedules = []
            for day_idx, start_min, end_min in c['meetings']:
                schedules.append({
                    "day": REV_DAY[day_idx],
                    "start": f"{start_min//60:02d}:{start_min%60:02d}",
                    "end": f"{end_min//60:02d}:{end_min%60:02d}"
                })
                
            formatted_courses.append({
                "code": c['course_code'],
                "name": c['title'],
                "teacher": c['teacher'],
                "session": c['session'],
                "units": c.get('units', 0),
                "schedules": schedules,
                "id": f"{c['course_code']}-{c['session']}"
            })
            
        return jsonify({
            "found": True,
            "courses": formatted_courses,
            "totalUnits": result['best_units']
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
