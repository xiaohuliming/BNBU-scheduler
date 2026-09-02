import requests
from bs4 import BeautifulSoup
import json
import time
import logging
import os
import re
from urllib.parse import urlparse

# Configure logging in
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
BASE_URL = os.getenv('ISPACE_BASE_URL', 'https://ispace.bnbu.edu.cn').rstrip('/')
LOGIN_URL = f"{BASE_URL}/login/index.php"
SERVICE_URL = f"{BASE_URL}/lib/ajax/service.php"

def get_login_token(session):
    try:
        response = session.get(LOGIN_URL, timeout=10)
        response.raise_for_status()
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
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        body_classes = set(soup.body.get('class', [])) if soup.body else set()
        login_error = soup.select_one('#loginerrormessage, .loginerrors')
        login_form = soup.find('form', attrs={'id': 'login'})
        logout_link = soup.find('a', href=lambda value: value and 'logout.php' in value)

        if login_error and login_error.get_text(' ', strip=True):
            logger.warning("Login failed: Invalid credentials.")
            return False
        if 'notloggedin' in body_classes or login_form:
            logger.warning("Login failed: iSpace still reports an unauthenticated session.")
            return False

        final_url = urlparse(response.url)
        expected_host = urlparse(BASE_URL).netloc
        on_dashboard = final_url.netloc == expected_host and final_url.path.rstrip('/') == '/my'
        if 'loggedin' in body_classes or logout_link or on_dashboard:
            logger.info("Login successful!")
            return True

        logger.warning("Login status unknown.")
        return False
    except Exception as e:
        logger.error(f"Login request failed: {e}")
        return False

def get_sesskey(session):
    try:
        response = session.get(f"{BASE_URL}/my/", timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Moodle exposes the session key inside M.cfg on authenticated pages.
        match = re.search(r'"sesskey"\s*:\s*"([^"\\]+)"', response.text)
        if match:
            return match.group(1)
                
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
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            message = data.get('message') or data.get('error') or data.get('errorcode') or 'Unknown error'
            if isinstance(message, dict):
                message = message.get('message') or message.get('errorcode') or 'Unknown error'
            return {"error": f"API Error: {message}"}

        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return {"error": "Unexpected response format"}

        first_result = data[0]
        if first_result.get('error'):
            exception = first_result.get('exception') or {}
            message = exception.get('message') if isinstance(exception, dict) else str(exception)
            return {"error": f"API Error: {message or 'Unknown error'}"}

        events = (first_result.get('data') or {}).get('events')
        if not isinstance(events, list):
            return {"error": "Unexpected response format"}
        return parse_events(events)

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
