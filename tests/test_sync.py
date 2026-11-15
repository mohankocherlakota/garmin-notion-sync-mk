import copy
import io
import unittest
from unittest.mock import patch
import sync


DAY = '2026-11-13'
TODAY = '2026-11-14'


class MappingTests(unittest.TestCase):
    def test_official_token_is_plain_text_jwt(self):
        with patch('sync.request', return_value='header.payload.signature') as request:
            client = sync.Garmin('SYNTHETIC_KEY')
        self.assertEqual(client.headers, {'Authorization': 'Bearer header.payload.signature'})
        self.assertTrue(request.call_args.kwargs['text_response'])

    def test_official_v2_nested_fields_and_units(self):
        row = {'movement': {'steps': {'value': 7123, 'distanceInMeters': 4200}},
               'calories': {'burnedActive': 321}, 'intensityMinutes': {'moderate': 0, 'vigorous': 12},
               'stress': {'avgLevel': -1}, 'heartRate': {'restingValue': None}}
        result = sync.mapped(row, sync.DAILY_FIELDS)
        self.assertEqual(result, {'Steps': 7123, 'Distance km': 4.2, 'Active calories kcal': 321,
                                  'Moderate minutes': 0, 'Vigorous minutes': 12})

    def test_missing_and_sentinel_values_not_fabricated(self):
        self.assertEqual(sync.mapped({'movement': {'steps': {'value': float('nan')}}}, sync.DAILY_FIELDS), {})
        self.assertFalse(sync.numeric(True))

    def test_evening_utc_rollover_and_dst(self):
        for utc, expected in [('2026-11-14 02:00:00', '2026-11-13T21:00:00-05:00'),
                              ('2026-07-14 02:00:00', '2026-07-13T22:00:00-04:00')]:
            self.assertEqual(sync.workout_time({'startTimeGMT': utc}).isoformat(), expected)

    def test_wrong_date_never_selected(self):
        self.assertEqual(sync.dated([{'calendarDate': TODAY, 'value': 1}], DAY), {})

    def test_sleep_and_weight_units(self):
        self.assertEqual(sync.mapped({'sleepTimeSeconds': 25200}, sync.SLEEP_FIELDS), {'Sleep hours': 7})
        self.assertAlmostEqual(sync.mapped({'weight': 81.6466}, {'Weight lb': ('weight', 1 / 2.2046226218)})['Weight lb'], 180, places=2)


class PlanTests(unittest.TestCase):
    def test_partial_preserves_manual_fields_and_positive_values(self):
        existing = {'Steps': {'number': 1000}, 'Weight lb': {'number': 180}, 'BMI': {'number': 25},
                    'Notes': {'rich_text': [{'text': {'content': 'manual'}}]}}
        original = copy.deepcopy(existing)
        props = sync.plan(DAY, {'Steps': 0, 'Weight lb': 170, 'BMI': 24, 'Stress avg': 20}, existing, TODAY)
        self.assertEqual(existing, original)
        self.assertFalse({'Steps', 'Weight lb', 'BMI', 'Notes'} & props.keys())
        self.assertEqual(props['Stress avg'], {'number': 20})

    def test_weight_bmi_optional_for_completion(self):
        data = dict.fromkeys(sync.REQUIRED, 10)
        self.assertEqual(sync.plan(DAY, data, {}, TODAY)['Data status']['select']['name'], 'Complete')
        data.update({'Workout count': 0, 'Workout minutes': 0})
        self.assertEqual(sync.plan(DAY, data, {}, TODAY)['Data status']['select']['name'], 'Complete - No Workout')

    def test_today_always_in_progress(self):
        self.assertEqual(sync.plan(TODAY, dict.fromkeys(sync.REQUIRED, 10), {}, TODAY)['Data status']['select']['name'], 'Sync Pending')

    def test_protected_date(self):
        with self.assertRaises(sync.SyncError):
            sync.plan('2026-11-09', {'Steps': 1}, {}, TODAY)

    def test_missing_metrics_never_null_or_zero(self):
        props = sync.plan(DAY, {'Steps': 1}, {}, TODAY)
        self.assertNotIn('Sleep hours', props)
        self.assertNotIn('Workout count', props)
        self.assertEqual(props['Data status']['select']['name'], 'Partial')


class CollectionTests(unittest.TestCase):
    def client(self, includes_activity=True, activities=None):
        client = object.__new__(sync.Garmin)
        def get(path, **params):
            if 'dailysummaries' in path:
                return {'data': [{'source': 'garmin', 'calendarDate': DAY, 'includesWellnessData': True,
                                  'includesActivityData': includes_activity, 'movement': {'steps': {'value': 50}}}]}
            if 'dailySleep' in path:
                return {'calendarDate': DAY, 'sleepTimeSeconds': 3600}
            if 'hrv-service' in path:
                return None
            if 'weight-service' in path:
                return {}
            return activities or []
        client.get = get
        return client

    def test_empty_activities_not_proof_of_no_workout(self):
        self.assertNotIn('Workout count', self.client(True).collect(DAY))

    def test_independent_summary_corroborates_no_workout(self):
        self.assertEqual(self.client(False).collect(DAY)['Workout count'], 0)

    def test_missing_duration_does_not_fabricate_zero(self):
        activity = {'activityId': 1, 'activityName': 'Test', 'startTimeGMT': '2026-11-14 02:00:00'}
        values = self.client(True, [activity]).collect(DAY)
        self.assertEqual(values['Workout count'], 1)
        self.assertNotIn('Workout minutes', values)
        self.assertIn('2026-11-13T21:00:00-05:00', values['Workout details'])


class NotionTests(unittest.TestCase):
    def test_repeated_sync_creates_one_row_and_preserves_notes(self):
        notion = object.__new__(sync.Notion)
        pages, creates = [], []
        def call(method, path, body=None, retry=True):
            if path.endswith('/query'):
                return {'results': copy.deepcopy(pages), 'has_more': False}
            if method == 'POST':
                self.assertFalse(retry)
                creates.append(1)
                pages.append({'id': 'fake', 'properties': copy.deepcopy(body['properties'])})
                return copy.deepcopy(pages[0])
            if method == 'PATCH':
                pages[0]['properties'].update(copy.deepcopy(body['properties']))
            return copy.deepcopy(pages[0])
        notion.call = call
        notion.sync(DAY, {'Steps': 12}, write=True)
        pages[0]['properties']['Notes'] = {'rich_text': []}
        notion.sync(DAY, {'Steps': 13}, write=True)
        self.assertEqual(len(creates), 1)
        self.assertEqual(pages[0]['properties']['Steps']['number'], 13)

    def test_duplicate_rows_block_writes(self):
        notion = object.__new__(sync.Notion)
        notion.call = lambda *a, **kw: {'results': [{'id': 'a'}, {'id': 'b'}], 'has_more': False}
        with self.assertRaises(sync.SyncError):
            notion.sync(DAY, {'Steps': 1}, write=True)

    def test_logs_exclude_health_values_and_workout_names(self):
        activity = {'activityId': 1, 'activityName': 'PRIVATE_WORKOUT', 'startTimeGMT': '2026-11-14 02:00:00'}
        output = io.StringIO()
        with patch('sys.stdout', output):
            CollectionTests().client(True, [activity]).collect(DAY)
        self.assertNotIn('PRIVATE_WORKOUT', output.getvalue())


if __name__ == '__main__':
    unittest.main()
