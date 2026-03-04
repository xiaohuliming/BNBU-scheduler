import requests
from bs4 import BeautifulSoup
import json
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
BASE_URL = "https://ispace.uic.edu.cn"
LOGIN_URL = f"{BASE_URL}/login/index.php"
SERVICE_URL = f"{BASE_URL}/lib/ajax/service.php"

def get_login_token(session):
    try:
        response = session.get(LOGIN_URL, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        token_input = soup.find('input', {'name': 'logintoken'})
        if token_input:
            return token_input['value']
    except Exception as e:
        logger.error(f"Error getting login token: {e}")
    return None

def login(session, username, password):
    logger.info(f"Attempting to log in user: {username}")
    token = get_login_token(session)
    
    payload = {
        'username': username,
        'password': password,
    }
    if token:
        payload['logintoken'] = token
        
    try:
        response = session.post(LOGIN_URL, data=payload, timeout=10)
        
        if "Dashboard" in response.text or "My courses" in response.text:
            logger.info("Login successful!")
            return True
        elif "Invalid login" in response.text:
            logger.warning("Login failed: Invalid credentials.")
            return False
        else:
            # Check if we are already logged in (redirected to dashboard)
            if response.url == f"{BASE_URL}/my/":
                logger.info("Login successful (redirected)!")
                return True
            logger.warning("Login status unknown.")
            return False
    except Exception as e:
        logger.error(f"Login request failed: {e}")
        return False

def get_sesskey(session):
    try:
        response = session.get(f"{BASE_URL}/my/", timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Try to find sesskey in M.cfg
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string and '"sesskey":' in script.string:
                start = script.string.find('"sesskey":"') + 11
                end = script.string.find('"', start)
                return script.string[start:end]
                
        # Alternative: look for logout link
        logout_link = soup.find('a', href=lambda x: x and 'logout.php' in x)
        if logout_link:
            href = logout_link['href']
            if 'sesskey=' in href:
                return href.split('sesskey=')[1].split('&')[0]
    except Exception as e:
        logger.error(f"Error getting sesskey: {e}")
            
    return None

def fetch_timeline(username, password):
    session = requests.Session()
    
    if not login(session, username, password):
        return {"error": "Login failed"}

    sesskey = get_sesskey(session)
    if not sesskey:
        return {"error": "Failed to retrieve session key"}

    logger.info("Fetching timeline data...")
    now = int(time.time())
    end_time = now + (180 * 24 * 60 * 60) # 6 months
    
    payload = [
        {
            "index": 0,
            "methodname": "core_calendar_get_action_events_by_timesort",
            "args": {
                "timesortfrom": int(now - (14 * 24 * 60 * 60)), # 2 weeks back
                "timesortto": int(end_time),
                "limitnum": 50
            }
        }
    ]
    
    params = {
        'sesskey': sesskey,
        'info': 'core_calendar_get_action_events_by_timesort'
    }
    
    try:
        response = session.post(SERVICE_URL, params=params, json=payload, timeout=10)
        data = response.json()
        
        if isinstance(data, list) and len(data) > 0:
            if 'error' in data[0] and data[0]['error']:
                return {"error": f"API Error: {data[0].get('exception', {}).get('message', 'Unknown error')}"}
            
            events = data[0]['data']['events']
            return parse_events(events)
        else:
            return {"error": "Unexpected response format"}
            
    except Exception as e:
        logger.error(f"Error fetching timeline: {e}")
        return {"error": str(e)}

import html

def parse_events(events):
    todo_list = []
    for event in events:
        name = event.get('name', '')
        # Remove " is due" suffix if present
        if name.endswith(" is due"):
            name = name[:-7]
        
        # Decode HTML entities
        name = html.unescape(name)

        todo_item = {
            'id': event.get('id'),
            'name': name,
            'course': event.get('course', {}).get('fullname'),
            'due_date': event.get('timesort'),
            'formatted_date': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event.get('timesort'))),
            'url': event.get('url'),
            'actionable': event.get('actionable', False)
        }
        todo_list.append(todo_item)
    return todo_list
