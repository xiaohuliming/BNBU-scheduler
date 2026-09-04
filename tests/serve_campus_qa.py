"""Run with the app's Python environment for disposable local UI/SDK checks."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['MAXCOURSE_SECRET_KEY'] = 'local-knowledge-qa-session-key'
os.environ['MAXCOURSE_PUBLIC_BASE_URL'] = 'http://127.0.0.1:5017'
import app
from werkzeug.security import generate_password_hash

with tempfile.TemporaryDirectory() as folder:
    app.DB_PATH = str(Path(folder) / 'users.db')
    app.init_db()
    with app.get_db() as conn:
        conn.execute('INSERT INTO users(username,password_hash) VALUES (?,?)',
                     ('knowledge-qa', generate_password_hash('local-qa-password-only')))
    app.app.run(host='127.0.0.1', port=5017, threaded=True, use_reloader=False)
