# iSpace To-Do List Crawler & Viewer

This tool crawls your iSpace timeline for upcoming deadlines and displays them in a clean interface.

## Setup

1.  Ensure you have Python installed.
2.  Install dependencies:
    ```bash
    pip install requests beautifulsoup4
    ```

## Usage

1.  **Update Data**: Run the crawler to fetch the latest to-do items.
    ```bash
    python crawl_ispace.py
    ```
    This will generate `todolist.json` and `todolist.js`.

2.  **View List**: Open `todolist.html` in your web browser.
    - You can open the file directly (double-click).
    - Or serve it via a local server: `python -m http.server` and go to `http://localhost:8000/todolist.html`.

## Configuration

Edit `crawl_ispace.py` to update your credentials if needed (currently hardcoded as provided).

## Notes

-   The crawler fetches tasks for the next 6 months.
-   It includes overdue tasks from the last 2 weeks.
