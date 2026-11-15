"""Official Garmin Personal API -> existing Notion data source. No third-party packages."""
import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo('America/New_York')
PROTECTED = {'2026-11-09'}
GARMIN = 'https://personal.connectapi.garmin.com'
NOTION = 'https://api.notion.com/v1'
DATABASE = '97a64b8374da496598f68c68fa1c51ab'
DATA_SOURCE = 'a3ba3dd3-d712-4553-84ab-4596d7df5866'
DAILY_FIELDS = {
    'Steps': ('movement.steps.value', 1),
    'Distance km': ('movement.steps.distanceInMeters', 1000),
    'Active calories kcal': ('calories.burnedActive', 1),
    'Moderate minutes': ('intensityMinutes.moderate', 1),
    'Vigorous minutes': ('intensityMinutes.vigorous', 1),
    'Resting HR bpm': ('heartRate.restingValue', 1),
    'Stress avg': ('stress.avgLevel', 1),
    'Body Battery high': ('bodyBattery.maxValue', 1),
    'Body Battery low': ('bodyBattery.minValue', 1),
    'Body Battery charged': ('bodyBattery.chargedValue', 1),
    'Body Battery drained': ('bodyBattery.drainedValue', 1),
}
SLEEP_FIELDS = {'Sleep hours': ('sleepTimeSeconds', 3600),
                'Deep sleep hours': ('deepSleepSeconds', 3600),
                'REM sleep hours': ('remSleepSeconds', 3600),
                'Sleep score': ('sleepScores.overall.value', 1)}
REQUIRED = set(DAILY_FIELDS) | set(SLEEP_FIELDS) | {'HRV ms', 'Workout count', 'Workout minutes'}


class SyncError(Exception):
    """Safe, static error messages only; never include response bodies."""


def request(method, url, headers, body=None, retry=True, text_response=False):
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(4 if retry else 1):
        try:
            req = Request(url, data=data, method=method,
                          headers={**headers, 'Content-Type': 'application/json'})
            with urlopen(req, timeout=40) as response:
                raw = response.read()
                if text_response:
                    return raw.decode().strip()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            transient = exc.code == 429 or exc.code >= 500
            if not transient or not retry or attempt == 3:
                raise SyncError(f'HTTP {exc.code}; check credential access or service availability') from None
            try:
                delay = min(60, max(1, float(exc.headers.get('Retry-After', 2 ** attempt))))
            except (TypeError, ValueError):
                delay = 2 ** attempt
        except (URLError, TimeoutError, OSError):
            if not retry or attempt == 3:
                raise SyncError('Network request failed; rerun safely after checking service availability') from None
            delay = 2 ** attempt
        except (ValueError, TypeError):
            raise SyncError('Unexpected API response format') from None
        time.sleep(delay)


def nested(obj, path):
    for part in path.split('.'):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def mapped(obj, fields):
    result = {}
    for name, (path, divisor) in fields.items():
        value = nested(obj, path)
        if numeric(value):
            result[name] = round(value / divisor, 4)
    return result


def dated(items, day, key='calendarDate'):
    matches = [x for x in items if isinstance(x, dict) and x.get(key) == day]
    if len(matches) > 1:
        raise SyncError('Ambiguous Garmin records for one date; no write performed')
    return matches[0] if matches else {}


def workout_time(item):
    raw = item.get('startTimeGMT')
    if not isinstance(raw, str):
        raise SyncError('Workout UTC timestamp missing; workout totals unavailable')
    try:
        stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(TZ)
    except ValueError:
        raise SyncError('Workout timestamp invalid; workout totals unavailable') from None


class Garmin:
    def __init__(self, key):
        token = request('POST', GARMIN + '/personalapitoken-service/personal-api/personal-api-key/token',
                        {'Authorization': 'ApiKey ' + key}, text_response=True)
        # Garmin's official client reads response.text, a raw JWT (not OAuth JSON).
        access = token
        if not isinstance(access, str) or len(access.split('.')) != 3:
            raise SyncError('Garmin token response missing JWT')
        self.headers = {'Authorization': 'Bearer ' + access}
        print('Garmin authentication: passed')

    def get(self, path, **params):
        return request('GET', GARMIN + path + ('?' + urlencode(params) if params else ''), self.headers)

    def collect(self, day):
        values, unavailable = {}, []
        summary = {}
        try:
            data = self.get('/usersummary-service/v2/dailysummaries', startDate=day, endDate=day)
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise SyncError('Daily summary format unavailable')
            rows = [x for x in data['data'] if str(x.get('source', '')).lower() == 'garmin']
            summary = dated(rows, day)
            if summary.get('includesWellnessData') is True:
                values.update(mapped(summary, DAILY_FIELDS))
            else:
                unavailable.append('wellness_not_uploaded')
        except SyncError:
            unavailable.append('daily_summary')
        try:
            sleep = self.get('/sleep-service/sleep/dailySleep', date=day)
            if isinstance(sleep, dict) and sleep.get('calendarDate') == day:
                values.update(mapped(sleep, SLEEP_FIELDS))
            else:
                unavailable.append('sleep')
        except SyncError:
            unavailable.append('sleep')
        try:
            hrv = self.get('/hrv-service/hrv/' + day)
            if nested(hrv, 'hrvSummary.calendarDate') == day:
                values.update(mapped(hrv, {'HRV ms': ('hrvSummary.lastNightAvg', 1)}))
        except SyncError:
            unavailable.append('hrv')
        try:
            weights = self.get(f'/weight-service/weight/range/{day}/{day}')
            row = dated((weights or {}).get('dailyWeightSummaries', []), day, 'summaryDate')
            latest = row.get('latestWeight') or {}
            if latest.get('calendarDate') == day:
                values.update(mapped(latest, {'Weight lb': ('weight', 1 / 2.2046226218), 'BMI': ('bmi', 1)}))
        except SyncError:
            unavailable.append('weight_optional')
        try:
            # Search filters use activity-local dates. Expand one day each side,
            # then select by UTC converted to America/New_York.
            target = date.fromisoformat(day)
            activities, seen = [], set()
            for offset in range(0, 10000, 99):
                batch = self.get('/activitylist-service/activities/search/activities', start=offset, limit=99,
                                 startDate=str(target - timedelta(days=1)), endDate=str(target + timedelta(days=1)))
                if not isinstance(batch, list):
                    raise SyncError('Workout response unavailable')
                for item in batch:
                    identifier = item.get('activityId')
                    if identifier is None or identifier in seen:
                        raise SyncError('Workout pagination ambiguous')
                    seen.add(identifier)
                    stamp = workout_time(item)
                    if str(stamp.date()) == day:
                        activities.append((item, stamp))
                if len(batch) < 99:
                    break
            else:
                raise SyncError('Workout pagination limit reached')
            if activities:
                values['Workout count'] = len(activities)
                if all(numeric(item.get('duration')) for item, _ in activities):
                    values['Workout minutes'] = round(sum(item['duration'] for item, _ in activities) / 60, 4)
                details = []
                for item, stamp in sorted(activities, key=lambda pair: pair[1]):
                    parts = [str(item.get('activityName') or 'Workout'), stamp.isoformat()]
                    for field, unit, factor in [('duration', 'min', 60), ('distance', 'km', 1000), ('calories', 'kcal', 1)]:
                        if numeric(item.get(field)):
                            parts.append(f'{item[field] / factor:.2f} {unit}')
                    details.append(' | '.join(parts))
                values['Workout details'] = '\n'.join(details)
            elif summary.get('includesWellnessData') is True and summary.get('includesActivityData') is False and day < str(datetime.now(TZ).date()):
                # Independent summary corroboration, never empty search alone.
                values.update({'Workout count': 0, 'Workout minutes': 0})
            else:
                unavailable.append('workout_absence_unconfirmed')
        except SyncError:
            unavailable.append('workouts')
        if not values:
            raise SyncError('No usable Garmin measurements returned; Notion untouched')
        print('Read date: ' + day)
        print('Garmin actual data: passed; unavailable groups: ' + ', '.join(unavailable))
        print('Fields available: ' + ', '.join(sorted(values)))
        print('Fields unavailable: ' + ', '.join(sorted(REQUIRED - values.keys())))
        return values


def rich_text(text):
    if len(text) > 100000:
        raise SyncError('Workout details exceed safe Notion size')
    return [{'type': 'text', 'text': {'content': text[i:i+1900]}} for i in range(0, len(text), 1900)]


def plan(day, values, existing, today):
    if day in PROTECTED:
        raise SyncError('Protected date; write forbidden')
    props = {}
    for name, value in values.items():
        if name in {'Weight lb', 'BMI'} and existing.get(name, {}).get('number') is not None:
            continue
        if name == 'Workout details':
            if value:
                props[name] = {'rich_text': rich_text(value)}
        elif numeric(value):
            # An explicit zero must not erase an existing positive measurement.
            if value == 0 and (existing.get(name, {}).get('number') or 0) > 0:
                continue
            props[name] = {'number': value}
    present = {k for k, v in existing.items() if numeric(v.get('number'))}
    present.update(k for k, v in props.items() if 'number' in v)
    workout_count = props.get('Workout count', existing.get('Workout count', {})).get('number')
    status = ('Sync Pending' if day == today else
              ('Complete - No Workout' if workout_count == 0 else 'Complete') if REQUIRED <= present else 'Partial')
    props['Data status'] = {'select': {'name': status}}
    # Do not change Notes, existing title, Date, or Source on an existing row.
    return props


class Notion:
    def __init__(self, token):
        self.headers = {'Authorization': 'Bearer ' + token, 'Notion-Version': '2025-09-03'}
        database = self.call('GET', '/databases/' + DATABASE)
        if DATA_SOURCE.replace('-', '') not in {x['id'].replace('-', '') for x in database.get('data_sources', [])}:
            raise SyncError('Configured Notion data source does not belong to tracker')
        schema = self.call('GET', '/data_sources/' + DATA_SOURCE)['properties']
        expected = {k: 'number' for k in REQUIRED | {'Weight lb', 'BMI'}}
        expected.update({'Day': 'title', 'Date': 'date', 'Data status': 'select', 'Workout details': 'rich_text'})
        if any(schema.get(k, {}).get('type') != t for k, t in expected.items()):
            raise SyncError('Notion schema mismatch; no writes performed')
        options = {x['name'] for x in schema['Data status']['select']['options']}
        if not {'Complete', 'Complete - No Workout', 'Partial', 'Sync Pending', 'Missing'} <= options:
            raise SyncError('Notion Data status options mismatch')
        print('Notion database and schema read: passed')

    def call(self, method, path, body=None, retry=True):
        return request(method, NOTION + path, self.headers, body, retry)

    def rows(self, day):
        result = self.call('POST', '/data_sources/' + DATA_SOURCE + '/query',
                           {'filter': {'property': 'Date', 'date': {'equals': day}}, 'page_size': 100})
        if result.get('has_more') or len(result['results']) > 1:
            raise SyncError('Duplicate date rows detected; refusing to choose or create a row')
        return result['results']

    def sync(self, day, values, write=False):
        rows = self.rows(day)
        existing = rows[0]['properties'] if rows else {}
        props = plan(day, values, existing, str(datetime.now(TZ).date()))
        print('Fields available: ' + ', '.join(sorted(values)))
        print('Fields unavailable: ' + ', '.join(sorted(REQUIRED - values.keys())))
        if not write:
            print('Read-only preflight passed; no writes performed')
            return
        if rows:
            page = self.call('PATCH', '/pages/' + rows[0]['id'], {'properties': props})
        else:
            props.update({'Day': {'title': rich_text(day)}, 'Date': {'date': {'start': day}}})
            # Never automatically retry a create: a timeout may have committed it.
            # A later workflow run queries the date before attempting another create.
            page = self.call('POST', '/pages', {'parent': {'type': 'data_source_id', 'data_source_id': DATA_SOURCE},
                                              'properties': props}, retry=False)
        saved = self.call('GET', '/pages/' + page['id'])['properties']
        if saved['Date']['date']['start'] != day:
            raise SyncError('Saved Notion date verification failed')
        for name, expected in props.items():
            if 'number' in expected and saved[name].get('number') != expected['number']:
                raise SyncError('Saved Notion measurement verification failed')
            if 'rich_text' in expected:
                actual = ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in saved[name]['rich_text'])
                wanted = ''.join(x['text']['content'] for x in expected['rich_text'])
                if actual != wanted:
                    raise SyncError('Saved workout details verification failed')
        if saved['Data status']['select']['name'] != props['Data status']['select']['name']:
            raise SyncError('Saved data status verification failed')
        for name in ['Notes', 'Weight lb', 'BMI']:
            if name in existing and name not in props and saved.get(name) != existing[name]:
                raise SyncError('Preserved manual field verification failed')
        check = self.rows(day)
        if len(check) != 1 or check[0]['id'] != page['id']:
            raise SyncError('Saved Notion date uniqueness verification failed')
        print('Notion write, readback, and date uniqueness: passed')
        print('Updated fields: ' + ', '.join(sorted(props)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dates', default='')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    required = ['GARMIN_API_KEY']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SyncError('Missing repository secrets: ' + ', '.join(missing))
    today = datetime.now(TZ).date()
    dates = [x.strip() for x in args.dates.split(',') if x.strip()] or [str(today - timedelta(days=i)) for i in (1, 2, 3)]
    if len(dates) > 31 or len(set(dates)) != len(dates):
        raise SyncError('Provide up to 31 unique dates')
    for day in dates:
        if str(date.fromisoformat(day)) != day or date.fromisoformat(day) > today or day in PROTECTED:
            raise SyncError('Invalid, future, or protected date; no writes performed')
    garmin = Garmin(os.environ['GARMIN_API_KEY'])
    # Read all requested dates before the first mutation.
    collected = [(day, garmin.collect(day)) for day in dates]
    if not os.environ.get('NOTION_TOKEN'):
        raise SyncError('Garmin read checks finished; missing repository secret: NOTION_TOKEN. Notion untouched')
    notion = Notion(os.environ['NOTION_TOKEN'])
    for day, values in collected:
        notion.sync(day, values, write=False)
    if args.write:
        for day, values in collected:
            print('Syncing date: ' + day)
            notion.sync(day, values, write=True)


if __name__ == '__main__':
    try:
        main()
    except SyncError as exc:
        print('Sync failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Tracebacks can expose response payloads and health information.
        print('Sync failed: unexpected response or configuration; no sensitive details logged', file=sys.stderr)
        sys.exit(1)
