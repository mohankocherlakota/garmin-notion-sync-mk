# Garmin → Notion daily health sync

Uses Garmin's **official Personal API**, Python's standard library, and GitHub Actions. No Garmin account password, scraping, Vercel deployment, or subscription required.

## Setup

1. In this repository open **Settings → Secrets and variables → Actions → New repository secret**.
   - `GARMIN_API_KEY`: your newly rotated Garmin Personal API key. GitHub secret names are case-insensitive, so an existing `garmin_api_key` works. Never paste keys into chat or commit them.
   - `NOTION_TOKEN`: an internal Notion integration token with read, insert, and update content capabilities. The Notion connection available in a chat does **not** automatically supply credentials to Actions.
2. In the existing **Garmin Health & Fitness Tracker**, open **••• → Connections → Add connection** and choose that Notion integration. No replacement database is created. The fitness reviews database is not changed by this daily sync.
3. Open **Actions → Garmin to Notion daily sync → Run workflow**. Leave `write` unchecked for read-only validation and use `2026-11-13,2026-11-14,2026-11-12` for the requested restoration.
4. After Garmin authentication, actual-data retrieval, Notion schema checks, and per-date read-only checks pass, run again with the same dates and check `write`. Every write is followed by a Notion readback and date uniqueness check.
5. Run a second time with `write` checked to verify that the same date rows are updated. A dry run or green unit test alone is not proof of a successful live sync.

Pushes affecting the sync or daily workflow run a **read-only** preflight against the previous three New York dates. Scheduled runs write at **08:17 America/New_York**, with automatic daylight saving time handling. Blank manual dates retry the previous three dates to catch delayed watch uploads. Manual runs default to read-only. All runs share one concurrency group.

GitHub Actions may delay scheduled runs; public repositories can have schedules disabled after prolonged inactivity. Check Actions and GitHub failure notifications periodically. Rotate the Garmin key before the configured expiration.

## Existing schema

The script validates the existing tracker and its configured data source at runtime before any write. It expects:

- `Day` (title), `Date` (date), `Data status` (select), `Workout details` (rich text).
- Number properties: `Steps`, `Distance km`, `Active calories kcal`, `Moderate minutes`, `Vigorous minutes`, `Resting HR bpm`, `HRV ms`, `Sleep hours`, `Sleep score`, `Deep sleep hours`, `REM sleep hours`, `Stress avg`, `Body Battery high`, `Body Battery low`, `Body Battery charged`, `Body Battery drained`, `Workout count`, `Workout minutes`, `Weight lb`, `BMI`.
- Existing status options: `Complete`, `Complete - No Workout`, `Partial`, `Sync Pending`, `Missing`.

`Notes`, existing titles, and existing `Source` values are never changed. Existing weight and BMI are preserved; absent ones may be filled from a same-date Garmin measurement. Missing, negative sentinel, non-finite, and null measurements are omitted. An incoming zero never replaces an existing positive value.

One row is queried by `Date` before every create or update. Existing duplicates stop the sync instead of choosing arbitrarily. Create requests are never automatically retried after an ambiguous failure; rerun the workflow so it queries the date first. Concurrency prevents overlapping runs from this repository; avoid simultaneous external writers creating rows for the same date.

Today's row uses `Sync Pending`. A past day is `Complete` only when all required daily fields are present; weight and BMI are optional. Empty activity search is insufficient to conclude no workout: an uploaded wellness summary must also explicitly report `includesActivityData=false` for a past day. Otherwise workout fields stay missing or unchanged and the day can remain partial. Existing valid values count toward completion. No placeholder `Missing` rows are created when every Garmin endpoint returns no usable measurements.

## API support and limits

Verified against Garmin's official [Personal API Swagger documentation](https://developer.garmin.com/gc-developer-program/personal/swagger-docs) and [official Python client](https://github.com/garmin/connect-personal-api-python):

| Source | Measurements |
| --- | --- |
| `/usersummary-service/v2/dailysummaries` | Nested movement, active calories, intensity minutes, resting HR, stress, Body Battery high/low/charged/drained |
| `/sleep-service/sleep/dailySleep?date=...` | Sleep duration, deep/REM, overall sleep score |
| `/hrv-service/hrv/{calendarDate}` | Last night's average HRV |
| `/activitylist-service/activities/search/activities` | Paginated workouts, name, UTC timestamp, seconds, meters, calories |
| `/weight-service/weight/range/{startDate}/{endDate}` | Same-date latest weight in kg converted to lb, BMI |

Daily summary ranges support up to one year per request; sleep-summary and Body Battery report ranges support up to 31 days. These range caps do not guarantee how far back your account has data. This script queries each requested calendar date separately and does not invent missing historical values. No universal numeric request quota was specified in the consulted public documentation. Requests are sequential and retry timeouts, 429, and 5xx with bounded backoff and `Retry-After` support. Missing device measurements remain unavailable.

Garmin authentication sends `Authorization: ApiKey ...` to the documented token endpoint. The response is a **raw JWT**, then data requests use `Authorization: Bearer ...`. [Garmin's FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/) states access tokens last up to 24 hours, capped by key expiration; no refresh token is supported. Each workflow obtains a new token in memory and stores no token cache.

Daily summary dates follow Garmin's account calendar. Keep the Garmin account timezone aligned with America/New_York. Workouts are queried over an expanded local-date window, then UTC timestamps are converted using America/New_York, including DST, to select the correct date. November 9, 2026 is blocked in both input validation and write planning.

## Validation and logs

```sh
python3 -m unittest discover -s tests -v
python3 sync.py --dates 2026-11-13,2026-11-14,2026-11-12
# After read-only success; credentials must already be in the environment:
python3 sync.py --dates 2026-11-13,2026-11-14,2026-11-12 --write
```

Tests use synthetic data and cover nested API fields, unit conversions, UTC rollover, DST, optional measurements, missing responses, preserved manual data, duplicate prevention, and repeated writes. These are regression tests, not a live-service success claim.

Logs include credential-presence errors, field names, availability, dates, and verification outcomes, never token values, measurement values, workout names, raw API responses, or Notion page content. This repository is public: never upload live health fixtures or debug response bodies as artifacts. Unexpected exceptions are sanitized.

## Live verification — November 14, 2026 (America/New_York)

Both required repository secret names were confirmed without displaying values. Official Garmin authentication and actual data retrieval passed. A read-only workflow validated the existing Notion database and schema before the controlled write.

[The first live write](https://github.com/mohankocherlakota/garmin-notion-sync-mk/actions/runs/43657676252) passed property-by-property readback and date uniqueness checks for November 13, November 14, and November 12. An independent read through the existing Notion connection confirmed one row per date with every required daily metric present: November 12 and 13 were `Complete`, and November 14 was `Sync Pending`. Garmin returned workout details for all three dates. Weight and BMI were not supplied by Garmin for these dates and existing entries were preserved. November 9 was not included in any sync.

[A second live write](https://github.com/mohankocherlakota/garmin-notion-sync-mk/actions/runs/43657733331) also passed. The independent Notion query returned the same three page IDs afterward, with exactly one record per date and unchanged completion states.

The daily workflow is active and scheduled for 08:17 America/New_York. Seventeen synthetic regression tests passed locally and in GitHub Actions. No Garmin account password, credentials, or live health measurements were committed.
