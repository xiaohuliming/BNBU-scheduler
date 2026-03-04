import os
import glob
import json
import sqlite3
import time
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from maximize_credits import load_timetable, maximize_credits, fmt_meeting
from crawler import fetch_timeline

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.urandom(24)  # For session management

# Database setup
DB_PATH = 'maxcourse.db'

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
            
        conn.commit()

init_db()

# Global cache for the dataframe
df_cache = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/ddl')
def ddl_page():
    return send_from_directory('.', 'ddl.html')

@app.route('/favicon.ico')
def favicon():
    return "", 204

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
    
    if user and check_password_hash(user['password_hash'], password):
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['display_name'] = user['display_name'] if user['display_name'] else user['ispace_username'] or user['username']
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
    for item in ddls:
        # Check if exists
        c.execute('SELECT id FROM todos WHERE user_id = ? AND ispace_id = ?', (user_id, item['id']))
        exists = c.fetchone()
        if not exists:
            c.execute('''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, item['id'], item['name'], item['course'], item['due_date'], item['url']))
            
    conn.commit()
    conn.close()
    
    session['user_id'] = user_id
    session['username'] = username
    session['display_name'] = display_name
    
    return jsonify({"success": True, "user": {"id": user_id, "username": username, "ispace_username": username, "display_name": display_name}})

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
    c.execute('SELECT * FROM todos WHERE user_id = ? ORDER BY due_date ASC', (session['user_id'],))
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
         
    result = fetch_timeline(ispace_user, ispace_pass)
    if isinstance(result, dict) and "error" in result:
        return jsonify(result), 400
        
    conn = get_db()
    c = conn.cursor()
    user_id = session['user_id']
    
    count = 0
    for item in result:
        c.execute('SELECT id FROM todos WHERE user_id = ? AND ispace_id = ?', (user_id, item['id']))
        if not c.fetchone():
            c.execute('''
                INSERT INTO todos (user_id, ispace_id, title, course, due_date, url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, item['id'], item['name'], item['course'], item['due_date'], item['url']))
            count += 1
            
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "added": count})

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
                "units": 0, # Note: We might need to look up units again if important
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
