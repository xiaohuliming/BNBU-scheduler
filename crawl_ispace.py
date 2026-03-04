import requests
from bs4 import BeautifulSoup
import json
import time
import sys

# Configuration
USERNAME = "t330025032"
PASSWORD = "Hzj050916"
BASE_URL = "https://ispace.uic.edu.cn"
LOGIN_URL = f"{BASE_URL}/login/index.php"
SERVICE_URL = f"{BASE_URL}/lib/ajax/service.php"

def get_login_token(session):
    response = session.get(LOGIN_URL)
    soup = BeautifulSoup(response.text, 'html.parser')
    token_input = soup.find('input', {'name': 'logintoken'})
    if token_input:
        return token_input['value']
    return None

def login(session):
    print("Attempting to log in...")
    token = get_login_token(session)
    
    payload = {
        'username': USERNAME,
        'password': PASSWORD,
    }
    if token:
        payload['logintoken'] = token
        
    response = session.post(LOGIN_URL, data=payload)
    
    if "Dashboard" in response.text or "My courses" in response.text:
        print("Login successful!")
        return True
    elif "Invalid login" in response.text:
        print("Login failed: Invalid credentials.")
        return False
    else:
        # Check if we are already logged in (redirected to dashboard)
        if response.url == f"{BASE_URL}/my/":
            print("Login successful (redirected)!")
            return True
        print("Login status unknown. Check output.")
        return False

def get_sesskey(session):
    # Retrieve sesskey from the dashboard or any authenticated page
    response = session.get(f"{BASE_URL}/my/")
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Try to find sesskey in M.cfg
    # It's usually inside a script tag: "sesskey":"..."
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string and '"sesskey":' in script.string:
            start = script.string.find('"sesskey":"') + 11
            end = script.string.find('"', start)
            return script.string[start:end]
            
    # Alternative: look for logout link which contains sesskey
    logout_link = soup.find('a', href=lambda x: x and 'logout.php' in x)
    if logout_link:
        href = logout_link['href']
        if 'sesskey=' in href:
            return href.split('sesskey=')[1].split('&')[0]
            
    return None

def fetch_timeline(session, sesskey):
    print("Fetching timeline data...")
    # Calculate timestamps
    now = int(time.time())
    # Fetch for next 6 months (approx)
    end_time = now + (180 * 24 * 60 * 60)
    
    # Moodle Web Service API payload for timeline
    # We use core_calendar_get_action_events_by_timesort
    
    payload = [
        {
            "index": 0,
            "methodname": "core_calendar_get_action_events_by_timesort",
            "args": {
                "timesortfrom": int(now - (14 * 24 * 60 * 60)),
                "timesortto": int(end_time),
                "limitnum": 50
            }
        }
    ]
    
    # URL parameters
    params = {
        'sesskey': sesskey,
        'info': 'core_calendar_get_action_events_by_timesort'
    }
    
    response = session.post(SERVICE_URL, params=params, json=payload)
    
    try:
        data = response.json()
        print("Response data:", json.dumps(data, indent=2))
        if isinstance(data, list) and len(data) > 0:
            if 'error' in data[0] and data[0]['error']:
                print(f"Error fetching timeline: {data[0]}")
                return None
            return data[0]['data']['events']
        else:
            print("Unexpected response format.")
            return None
    except json.JSONDecodeError:
        print("Failed to decode JSON response.")
        print(response.text[:500])
        return None

def save_todolist(events):
    if not events:
        print("No events found.")
        return
        
    todo_list = []
    for event in events:
        todo_item = {
            'id': event.get('id'),
            'name': event.get('name'),
            'course': event.get('course', {}).get('fullname'),
            'due_date': event.get('timesort'),
            'formatted_date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event.get('timesort'))),
            'url': event.get('url'),
            'actionable': event.get('actionable', False)
        }
        todo_list.append(todo_item)
        
    with open('todolist.json', 'w', encoding='utf-8') as f:
        json.dump(todo_list, f, indent=4, ensure_ascii=False)
    
    # Also save as JS file for local viewing without server
    with open('todolist.js', 'w', encoding='utf-8') as f:
        f.write(f"const todoData = {json.dumps(todo_list, indent=4, ensure_ascii=False)};")

    print(f"Saved {len(todo_list)} items to todolist.json and todolist.js")

def main():
    session = requests.Session()
    
    if login(session):
        sesskey = get_sesskey(session)
        if sesskey:
            print(f"Sesskey found: {sesskey}")
            events = fetch_timeline(session, sesskey)
            save_todolist(events)
        else:
            print("Failed to retrieve sesskey.")
    else:
        print("Login failed.")

if __name__ == "__main__":
    main()
