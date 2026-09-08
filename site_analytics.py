"""Read-only, consistent UTC+8 analytics for the public dashboard.

Every filtered card and breakdown uses the same raw-event window. Historical
rollups are deliberately not mixed with raw events or summed to estimate UV.
Only aggregates leave this module; visitor IDs and raw error URLs stay private.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

BEIJING = timezone(timedelta(hours=8))
BOT_SQL = """(lower(COALESCE(user_agent, '')) LIKE '%bot%'
 OR lower(COALESCE(user_agent, '')) LIKE '%spider%'
 OR lower(COALESCE(user_agent, '')) LIKE '%crawl%'
 OR lower(COALESCE(user_agent, '')) LIKE '%slurp%'
 OR lower(COALESCE(user_agent, '')) LIKE '%headlesschrome%'
 OR lower(COALESCE(user_agent, '')) LIKE '%bingpreview%')"""
DEVICE_SQL = f"""CASE WHEN {BOT_SQL} THEN 'bot'
 WHEN COALESCE(user_agent, '') = '' THEN 'unknown'
 WHEN user_agent LIKE '%iPad%' OR user_agent LIKE '%Tablet%'
   OR (user_agent LIKE '%Android%' AND user_agent NOT LIKE '%Mobile%') THEN 'tablet'
 WHEN user_agent LIKE '%Mobile%' OR user_agent LIKE '%Android%'
   OR user_agent LIKE '%iPhone%' OR user_agent LIKE '%iPod%' THEN 'mobile'
 ELSE 'desktop' END"""
# Exact footprint of the repository's historical three-byte proxy fixtures.
# Keep source rows untouched; production tests must stop emitting these events.
FIXTURE_SQL = """(action='proxy' AND bytes=3 AND user_id IS NULL
 AND host='upos-sz-mirrorcosov.bilivideo.com')"""
DOWNLOAD_SQL = "action IN ('proxy','merge','batch')"
PLATFORM_SQL = """CASE lower(COALESCE(platform,''))
 WHEN 'xhslink' THEN 'xiaohongshu' WHEN 'xhs' THEN 'xiaohongshu'
 WHEN 'x' THEN 'twitter' WHEN 'b23' THEN 'bilibili'
 WHEN '' THEN 'unknown' ELSE lower(platform) END"""


def utc_string(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def window_from_args(args, now=None):
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING).replace(microsecond=0)
    today = now.date()
    try:
        if args.get('start') or args.get('end'):
            start = date.fromisoformat(args.get('start', ''))
            end = date.fromisoformat(args.get('end', ''))
        else:
            days = int(args.get('days', '30'))
            start, end = today - timedelta(days=days - 1), today
        days = (end - start).days + 1
        if not 1 <= days <= 365 or end > today:
            raise ValueError()
        bots = args.get('exclude_bots', '1')
        if bots not in ('0', '1'):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise ValueError('请选择不超过今天、长度为 1 至 365 天的有效日期范围。') from None
    lower = datetime.combine(start, time.min, BEIJING)
    upper = min(datetime.combine(end + timedelta(days=1), time.min, BEIJING), now)
    previous_lower, previous_upper = lower - timedelta(days=days), upper - timedelta(days=days)
    return {
        'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
        'until': upper.isoformat(), 'today': today.isoformat(), 'partialToday': end == today,
        'previousStart': previous_lower.date().isoformat(),
        'previousEnd': (end - timedelta(days=days)).isoformat(),
        'excludeBots': bots == '1',
    }, (utc_string(lower), utc_string(upper)), (utc_string(previous_lower), utc_string(previous_upper)), now


def rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params)]


def ratio(numerator, denominator):
    return round(numerator / denominator * 100, 2) if denominator else None


def traffic_totals(conn, bounds, exclude_bots):
    condition = f"created_at >= ? AND created_at < ?{' AND NOT ' + BOT_SQL if exclude_bots else ''}"
    total = dict(conn.execute(f"""SELECT COUNT(*) AS views,
        COUNT(DISTINCT NULLIF(visitor_id,'')) AS visitors,
        COUNT(NULLIF(visitor_id,'')) AS identifiedViews,
        COUNT(DISTINCT user_id) AS registeredVisitors
        FROM page_views WHERE {condition}""", bounds).fetchone())
    new_visitors = conn.execute(f"""SELECT COUNT(*) FROM (
        SELECT visitor_id, MIN(created_at) AS first_seen FROM page_views
        WHERE NULLIF(visitor_id,'') IS NOT NULL {'AND NOT ' + BOT_SQL if exclude_bots else ''}
        GROUP BY visitor_id) WHERE first_seen >= ? AND first_seen < ?""", bounds).fetchone()[0]
    total.update(newVisitors=new_visitors, returningVisitors=total['visitors'] - new_visitors,
                 viewsPerVisitor=round(total['identifiedViews'] / total['visitors'], 2) if total['visitors'] else None)
    return total


def media_totals(conn, bounds):
    row = dict(conn.execute(f"""SELECT
        SUM(action='resolve') AS resolves, SUM(action='resolve' AND success=1) AS resolveOk,
        SUM({DOWNLOAD_SQL}) AS downloads, SUM({DOWNLOAD_SQL} AND success=1) AS downloadOk,
        SUM(CASE WHEN {DOWNLOAD_SQL} THEN bytes ELSE 0 END) AS bytes,
        SUM(action='merge') AS merges, SUM(action='batch') AS batches,
        SUM(action='proxy') AS singles
        FROM media_dl_events WHERE created_at >= ? AND created_at < ? AND NOT {FIXTURE_SQL}""", bounds).fetchone())
    row = {key: value or 0 for key, value in row.items()}
    row.update(resolveRate=ratio(row['resolveOk'], row['resolves']),
               downloadRate=ratio(row['downloadOk'], row['downloads']))
    return row


def percentile(values, proportion):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * proportion) - 1)]


def classify_error(message):
    message = (message or '').lower()
    if 'client disconnected' in message or 'generatorexit' in message:
        return 'cancelled', '客户端中断'
    if any(word in message for word in ('timeout', 'timed out', '超时')):
        return 'timeout', '请求超时'
    if any(word in message for word in ('cookie', 'login', 'sign in', '验证', '登录')):
        return 'session', '登录或访问验证'
    if any(word in message for word in ('403', '412', 'forbidden', '风控', '拒绝')):
        return 'blocked', '源站拒绝访问'
    if any(word in message for word in ('404', '410', 'not found', '失效', '删除')):
        return 'unavailable', '内容失效或不可用'
    if any(word in message for word in ('dns', 'resolve host', 'name or service', 'network', 'connection')):
        return 'network', '网络连接问题'
    if any(word in message for word in ('unsupported', '不支持', 'url 为空')):
        return 'unsupported', '不支持的链接'
    return 'other', '其他解析或传输异常'


def referrer_group(value, self_hosts, known=True):
    if value is None:
        return 'unknown', None
    if not value.strip():
        return ('direct' if known else 'legacy'), None
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower().removeprefix('www.')
        if parsed.scheme not in ('http', 'https') or not host:
            return 'unknown', None
    except ValueError:
        return 'unknown', None
    if host in self_hosts or host in ('bnbscheduler.top', 'localhost', '127.0.0.1', '::1', '0.0.0.0') or host.endswith('.bnbscheduler.top'):
        return ('internal' if known else 'legacy'), None
    # Report domains only, never signed URLs or query parameters.
    return 'external', host


def build_dashboard(conn, args, now=None, self_hosts=()):
    conn.row_factory = sqlite3.Row
    window, bounds, previous_bounds, now = window_from_args(args, now)
    bot_clause = ' AND NOT ' + BOT_SQL if window['excludeBots'] else ''
    traffic_where = 'created_at >= ? AND created_at < ?' + bot_clause
    media_where = f'created_at >= ? AND created_at < ? AND NOT {FIXTURE_SQL}'
    traffic = traffic_totals(conn, bounds, window['excludeBots'])
    # A current account setting, independent of event dates and visitor filters.
    # Count each account once; do not expose addresses or account identifiers.
    subscriptions = {'ddlEmailEnabled': conn.execute(
        'SELECT COUNT(*) FROM users WHERE email_notifications_enabled = 1'
    ).fetchone()[0]}
    traffic['previous'] = traffic_totals(conn, previous_bounds, window['excludeBots'])
    media = media_totals(conn, bounds)
    media['previous'] = media_totals(conn, previous_bounds)
    traffic['pages'] = rows(conn, f"""SELECT COALESCE(NULLIF(view_name,''),'unknown') AS name,
        COUNT(*) AS views, COUNT(DISTINCT NULLIF(visitor_id,'')) AS visitors
        FROM page_views WHERE {traffic_where} GROUP BY name ORDER BY views DESC, name""", bounds)
    traffic['devices'] = rows(conn, f"""SELECT {DEVICE_SQL} AS name, COUNT(*) AS views
        FROM page_views WHERE {traffic_where} GROUP BY name ORDER BY views DESC""", bounds)
    traffic['hourly'] = rows(conn, f"""SELECT CAST(strftime('%H',datetime(created_at,'+8 hours')) AS INTEGER) AS hour,
        COUNT(*) AS views FROM page_views WHERE {traffic_where} GROUP BY hour""", bounds)
    hourly_map = {r['hour']: r['views'] for r in traffic['hourly']}
    traffic['hourly'] = [{'hour': hour, 'views': hourly_map.get(hour, 0)} for hour in range(24)]
    sources, external = Counter(), Counter()
    page_columns = {row[1] for row in conn.execute('PRAGMA table_info(page_views)')}
    known_column = 'referrer_known' if 'referrer_known' in page_columns else '0'
    for row in conn.execute(f'SELECT referrer,{known_column} AS known,COUNT(*) AS n FROM page_views WHERE {traffic_where} GROUP BY referrer,known', bounds):
        kind, host = referrer_group(row['referrer'], set(self_hosts), bool(row['known']))
        sources[kind] += row['n']
        if host:
            external[host] += row['n']
    traffic['sources'] = [{'name': name, 'views': sources[name]} for name in ('direct', 'internal', 'external', 'legacy', 'unknown')]
    traffic['referrers'] = [{'host': name, 'views': count} for name, count in external.most_common(12)]
    traffic['otherReferrers'] = sum(external.values()) - sum(r['views'] for r in traffic['referrers'])
    traffic['excludedBots'] = conn.execute(f'SELECT COUNT(*) FROM page_views WHERE created_at >= ? AND created_at < ? AND {BOT_SQL}', bounds).fetchone()[0]

    media['platforms'] = rows(conn, f"""SELECT {PLATFORM_SQL} AS name,
        SUM(action='resolve') AS resolves, SUM(action='resolve' AND success=1) AS resolveOk,
        SUM({DOWNLOAD_SQL}) AS downloads, SUM({DOWNLOAD_SQL} AND success=1) AS downloadOk,
        SUM(CASE WHEN {DOWNLOAD_SQL} THEN bytes ELSE 0 END) AS bytes
        FROM media_dl_events WHERE {media_where} GROUP BY name ORDER BY resolves DESC, downloads DESC, name""", bounds)
    timings = [row[0] for row in conn.execute(f"SELECT elapsed_ms FROM media_dl_events WHERE {media_where} AND action='resolve' AND elapsed_ms >= 0", bounds)]
    media.update(latencyP50=percentile(timings, .5), latencyP95=percentile(timings, .95), latencySamples=len(timings))
    reasons, platforms = {}, {}
    for row in conn.execute(f"SELECT error,{PLATFORM_SQL} AS platform,created_at FROM media_dl_events WHERE {media_where} AND success=0", bounds):
        key, label = classify_error(row['error'])
        entry = reasons.setdefault(key, {'name': key, 'label': label, 'count': 0, 'lastSeen': row['created_at']})
        entry['count'] += 1
        entry['lastSeen'] = max(entry['lastSeen'], row['created_at'])
        platforms.setdefault(key, set()).add(row['platform'])
    media['errors'] = sorted([dict(entry, platforms=sorted(platforms[key])) for key, entry in reasons.items()], key=lambda entry: -entry['count'])
    media['excludedTests'] = conn.execute(f'SELECT COUNT(*) FROM media_dl_events WHERE created_at >= ? AND created_at < ? AND {FIXTURE_SQL}', bounds).fetchone()[0]

    pv = rows(conn, f"""SELECT date(created_at,'+8 hours') AS day, COUNT(*) AS views,
        COUNT(DISTINCT NULLIF(visitor_id,'')) AS visitors FROM page_views WHERE {traffic_where} GROUP BY day""", bounds)
    mv = rows(conn, f"""SELECT date(created_at,'+8 hours') AS day,
        SUM(action='resolve') AS resolves, SUM(action='resolve' AND success=1) AS resolveOk,
        SUM({DOWNLOAD_SQL}) AS downloads, SUM({DOWNLOAD_SQL} AND success=1) AS downloadOk,
        SUM(CASE WHEN {DOWNLOAD_SQL} THEN bytes ELSE 0 END) AS bytes
        FROM media_dl_events WHERE {media_where} GROUP BY day""", bounds)
    pv_map, mv_map = {r['day']: r for r in pv}, {r['day']: r for r in mv}
    coverage = {}
    for key, table in (('traffic', 'page_views'), ('media', 'media_dl_events')):
        extra = " ,COUNT(DISTINCT NULLIF(visitor_id,'')) AS visitors" if key == 'traffic' else ''
        condition = f" WHERE action IN ('resolve','proxy','merge','batch') AND NOT {FIXTURE_SQL}" if key == 'media' else ''
        coverage[key] = dict(conn.execute(f'SELECT MIN(created_at) AS firstEvent,MAX(created_at) AS latestEvent,COUNT(*) AS records{extra} FROM {table}{condition}').fetchone())
    daily = []
    for i in range(window['days']):
        day = (date.fromisoformat(window['start']) + timedelta(days=i)).isoformat()
        entry = {'day': day, 'views': 0, 'visitors': 0, 'resolves': 0, 'resolveOk': 0,
                 'downloads': 0, 'downloadOk': 0, 'bytes': 0}
        entry.update(pv_map.get(day, {})); entry.update(mv_map.get(day, {}))
        for source, keys in (('traffic', ('views', 'visitors')), ('media', ('resolves', 'resolveOk', 'downloads', 'downloadOk', 'bytes'))):
            first = coverage[source]['firstEvent']
            first_day = (datetime.fromisoformat(first).replace(tzinfo=timezone.utc).astimezone(BEIJING).date().isoformat() if first else None)
            if first_day is None or day < first_day:
                for key in keys:
                    entry[key] = None
        daily.append(entry)
    return {'generatedAt': now.isoformat(), 'timezone': 'Asia/Shanghai', 'window': window,
            'traffic': traffic, 'media': media, 'daily': daily, 'coverage': coverage,
            'subscriptions': subscriptions}


def create_analytics_blueprint(db_path_provider, runtime_provider=None):
    blueprint = Blueprint('analytics_dashboard', __name__)

    @blueprint.get('/api/analytics/dashboard')
    def dashboard():
        try:
            window_from_args(request.args)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        try:
            # Explicit read-only connection and one snapshot for all panels.
            uri = Path(db_path_provider()).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                result = build_dashboard(conn, request.args,
                    self_hosts={(request.host.split(':')[0]).removeprefix('www.'), '103.106.188.87'})
                conn.rollback()
        except sqlite3.Error:
            return jsonify({'error': '统计数据暂时无法读取，请稍后重试。'}), 503
        result['runtime'] = dict(runtime_provider()) if runtime_provider else None
        response = jsonify(result)
        response.headers['Cache-Control'] = 'no-store'
        return response

    return blueprint
