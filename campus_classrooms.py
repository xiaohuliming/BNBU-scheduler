"""Read-only live classroom tools backed by the website's shared query logic."""

import re
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8))
TIMEZONE = 'Asia/Shanghai'
CLASSROOM_TOOL_DEFINITIONS = [
    {
        'name': 'find_free_classrooms',
        'description': (
            'Query live platform use-intent counts and weekly-timetable-free rooms. '
            'Use this tool, NOT document search, for now/today/future classroom availability. '
            'All dates/times use Asia/Shanghai. Omitted date means today; omitted start '
            'means now on today only; a future date requires start. Omitted end means '
            '60 minutes after start, capped at midnight. Explicit intervals have no '
            '4-hour limit. The available date range is returned in query. '
            'End is exclusive and may be 24:00. Filter building/room by exact name. '
            'Follow next_offset for all matches; results may change between pages. '
            'Counts are self-reported plans/check-ins, not a sensor or reservation. '
            'Never claim a room is physically empty, unlocked, or officially reserved. '
            'Timetable does not verify holidays or temporary room changes.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'date': {'type': 'string', 'description': 'YYYY-MM-DD, today through the next 14 days.'},
                'start': {'type': 'string', 'description': 'HH:MM, inclusive; default current Beijing minute.'},
                'end': {'type': 'string', 'description': 'HH:MM, exclusive; 24:00 is allowed for midnight.'},
                'building': {'type': 'string', 'maxLength': 32, 'description': 'Exact building code, e.g. T8; case-insensitive.'},
                'room': {'type': 'string', 'maxLength': 64, 'description': 'Exact room, e.g. T8-307; case-insensitive.'},
                'exclude_intents': {'type': 'boolean', 'default': False, 'description': 'Only return rooms with no active overlapping platform use intents. This does NOT establish physical vacancy.'},
                'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000, 'default': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 20},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_classroom_schedule',
        'description': (
            'Read a room\'s current-semester WEEKLY timetable, with all seven weekdays. '
            'This is a timetable snapshot, not live physical occupancy or an official '
            'booking calendar. For a dated free interval and current use-intent counts, '
            'call find_free_classrooms with room, date, start and end.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {'room': {'type': 'string', 'minLength': 1, 'maxLength': 64}},
            'required': ['room'],
            'additionalProperties': False,
        },
    },
]


def _clock(value, *, allow_midnight=False):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
        raise ValueError('Time must be HH:MM in Asia/Shanghai')
    if allow_midnight and value == '24:00':
        return 1440
    hours, minutes = map(int, value.split(':'))
    if hours > 23 or minutes > 59:
        raise ValueError('Invalid time')
    return hours * 60 + minutes


def _label(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError('Invalid room/building')
    value = value.strip().upper()
    if not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)*', value):
        raise ValueError('Invalid room/building')
    return value


class ClassroomTools:
    def __init__(self, *, now, free_rooms, schedule, source, max_days_ahead):
        self.now = now
        self.free_rooms = free_rooms
        self.schedule = schedule
        self.source = source
        self.max_days_ahead = max_days_ahead

    def _metadata(self, now, *, live_intents):
        return {
            'as_of': now.isoformat(timespec='seconds'),
            'timezone': TIMEZONE,
            'source': self.source(),
            'timetable_basis': 'current_semester_weekly_snapshot',
            'intent_basis': 'live_self_reported_platform_records' if live_intents else 'not_included',
            'physical_occupancy': 'unknown',
            'notice': '课表空闲不代表现场无人、门已开放或已获预约。使用意向仅为平台自报；临时调课、假期与场地开放情况未核验。',
        }

    def find_free_classrooms(self, date=None, start=None, end=None, building=None,
                             room=None, exclude_intents=False, offset=0, limit=20):
        now = self.now().astimezone(BEIJING)
        today = now.date()
        use_date = today
        if date is not None:
            if not isinstance(date, str):
                raise ValueError('Date must be YYYY-MM-DD')
            try:
                use_date = datetime.strptime(date, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('Date must be YYYY-MM-DD')
            if use_date.isoformat() != date:
                raise ValueError('Date must be YYYY-MM-DD')
        last_date = today + timedelta(days=self.max_days_ahead)
        if not today <= use_date <= last_date:
            raise ValueError(f'Date must be between {today} and {last_date} in Asia/Shanghai')
        if start is None and use_date != today:
            raise ValueError('Specify start when querying a future date')
        start_min = _clock(start) if start is not None else now.hour * 60 + now.minute
        end_min = _clock(end, allow_midnight=True) if end is not None else min(start_min + 60, 1440)
        if end_min <= start_min:
            raise ValueError('End must be later than start on the same date')
        if use_date == today and end_min <= now.hour * 60 + now.minute:
            raise ValueError('The requested interval has already ended in Asia/Shanghai')
        if type(exclude_intents) is not bool:
            raise ValueError('exclude_intents must be a boolean')
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError('offset must be an integer from 0 to 10000')
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be an integer from 1 to 50')
        building = _label(building, 32) if building is not None else None
        room = _label(room, 64) if room is not None else None
        result = self.free_rooms(use_date, start_min, end_min, user_id=None, now=now)
        if building and not any(row['building'] == building for row in result['buildings']):
            raise KeyError('Building not found in the classroom finder')
        if room:
            # Distinguish an unknown room from a known but timetabled-busy room.
            known = self.schedule(room)
            if not any(row['building'] == known['building'] for row in result['buildings']):
                raise KeyError('Room not included in the classroom finder')
            if building and known['building'] != building:
                raise ValueError('Room does not belong to the requested building')
        matches = []
        for row in result['rooms']:
            if (building and row['building'] != building) or (room and row['room'] != room):
                continue
            if exclude_intents and row['intent']['records']:
                continue
            # Explicit allowlist: never include "my", user ids or intent ids, even
            # when the request also happens to carry a logged-in browser cookie.
            public = {key: row[key] for key in ('room', 'building', 'next_busy', 'previous_busy')}
            public['free_until'] = row['next_busy']['start'] if row['next_busy'] else None
            public['no_later_class_in_timetable'] = row['next_busy'] is None
            public['intent'] = {key: row['intent'][key] for key in (
                'records', 'people', 'planned_people', 'checked_in_people')}
            matches.append(public)
        query = {key: result['query'][key] for key in ('day', 'day_label', 'date', 'start', 'end')}
        query.update({
            'date_range': {'from': today.isoformat(), 'to': last_date.isoformat()},
            'state': 'ongoing' if use_date == today and start_min <= now.hour * 60 + now.minute else 'future',
            'filters': {'building': building, 'room': room, 'exclude_intents': exclude_intents},
        })
        selected = matches[offset:offset + limit]
        return dict(self._metadata(now, live_intents=True), query=query, rooms=selected,
                    total=len(matches), returned=len(selected), offset=offset,
                    next_offset=offset + limit if offset + limit < len(matches) else None,
                    free_until_note='null means no later class is listed that day; it is not a building closing time.')

    def get_classroom_schedule(self, room):
        now = self.now().astimezone(BEIJING)
        return dict(self.schedule(_label(room, 64)), **self._metadata(now, live_intents=False))
