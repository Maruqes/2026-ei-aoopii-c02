# Speechmatics: usage and credits

Investigation on 2026-10-03. Only GET queries were made to Speechmatics;
no audio was sent, no job was created and the configuration was not changed. The key
provided is not included in this document.

## Result

- The documented public Usage API reports **Batch**, not Realtime or balance.
- The portal shows Realtime and Batch under Usage, and credits under Billing.
- No documented public endpoint was found for querying credits.
  This does not prove that a private or contractual API does not exist.
- In the additional investigation, the internal portal route
  `GET https://portal.speechmatics.com/api/credits` was found. The client reads `data.credits`.
  The route returned HTTP 302 to `/login`, both without authentication and with the
  transcription key in `Authorization: Bearer`. An authenticated portal session is
  missing to validate the returned balance.
- The documented Management API manages projects and keys, with a management token;
  it does not document balance or billing operations.
- There is an important difference between the documentation and the observed behaviour:
  an explicit `until` with today's date returned jobs from today in EU1.

## Real queries with the provided key

| Endpoint/period | HTTP | Batch result |
| --- | --- | --- |
| EU1 `/v2/usage`, without dates | 200 | 107 jobs; 37.328484 h; 2026-06-19 to 2026-10-02 |
| EU1 `since=2026-10-01&until=2026-10-02` | 200 | `summary=null`, `details=null` |
| EU1 `since=2026-10-01&until=2026-10-03` | 200 | 4 jobs; 0.08828889 h |
| EU1 `since=2026-10-03&until=2026-10-03` | 200 | The same 4 jobs; 0.08828889 h |
| EU1 `since=2026-10-02&until=2026-10-02` | 200 | `summary=null`, `details=null` |
| EU1 `since=2026-10-02&until=2026-10-03` | 200 | The same 4 jobs; 0.08828889 h |
| EU2 `/v2/usage`, without dates | 401 | No access with this key |
| US1 `/v2/usage`, without dates | 200 | `summary=null`, `details=null`; start 1970-01-01 |

EU1 is `https://eu1.asr.api.speechmatics.com`. The 107 jobs represent about
37h 19m 43s; today's 4, about 5m 18s, all Melia 1. The history in EU1
includes 106 Melia 1 jobs and 1 Enhanced. The usage responses did not contain
price, balance or credit fields, nor credit/balance/quota/usage headers.

The empty result in US1 demonstrates that changing endpoint can change the
visible consumption; it must not be presented as the account's free balance. The portal
endpoint redirected to login; the authenticated balance was not observed.

The documentation says that the current day is excluded. The tests above demonstrate that
EU1 accepts and returns data from that day when `until` is explicit. The update latency
was not measured and this behaviour cannot be guaranteed in other
regions or accounts.

## Cause of the weak presentation in the project

1. `app/speechmatics_usage.py:fetch_speechmatics_usage` forces `until=yesterday`.
   `app/main.py:get_speechmatics_key_usages` adds only local Batch WAVs
   completed today. `/keys` receives and shows only that Batch consumption.
2. There is `/v1/speechmatics/realtime-usage`, but `/keys` does not use it.
3. `app/realtime.py:bridge` only persists `realtime_seconds` when finishing/failing a
   unit, through `DataRepository.finish_realtime_unit`. An active unit
   does not update the persisted counter; a crash can lose that interval.
4. `DataRepository.local_realtime_hours` aggregates the whole history, with no filter by
   period; Batch uses the current UTC month by default. The values are not comparable
   without aligning the dates.
5. The frame counter increases before `upstream.send` finishes. To estimate
   consumption, it is preferable to count completed sends, recognizing that sending audio
   does not prove billing by the provider.

## Proposed minimal change

- Show Batch and Realtime separately in `/keys`, with seconds and period.
- For Batch, allow an explicit query up to today and identify today's data
  as provisional. Do not add local WAVs from today to the same total: that would duplicate
  jobs already reported. If delays need to be supplemented, use a separate
  estimate, without presenting it as a reconciled total.
- For Realtime, periodically store the cumulative total of audio sent,
  per unit/key, and do a final flush on closing. Reuse `realtime_seconds`,
  without a new table. The update and the closing must preserve the highest observed value
  to prevent a delayed checkpoint from making the counter go backwards.
- Filter Realtime by the displayed period, distinguishing local consumption from
  official reporting. A recording that goes from Realtime to Batch fallback can
  consume both products; do not eliminate one of the portions as a duplicate.
- Show credits as **unavailable through the public API consulted**, with a link
  to Billing. Do not convert an hours budget into a credit balance.
- If a monetary estimate is needed, use the initial balance confirmed in the
  portal and the account's rates/discounts. The formula is initial balance minus consumption
  since the date of that reading, with a separate rate per product/model. Use by other
  applications, grants, expirations and price changes prevent an exact balance from
  local counters alone.
- Several keys of the same workspace may share the balance; do not add balances or
  assign an independent grant to each key.

## Official sources

- [Usage: API only Batch; portal Realtime and Batch](https://docs.speechmatics.com/administration/usage)
- [Usage reporting: UTC dates and omission of `until`](https://docs.speechmatics.com/speech-to-text/batch/usage)
- [GET /usage reference](https://docs.speechmatics.com/api-ref/batch/get-usage-statistics)
- [Billing: credits, PAYG and the need for the Admin role](https://docs.speechmatics.com/administration/billing)
- [Management API: projects and API keys](https://docs.speechmatics.com/api-ref/management/management-api)
- [Management tokens: available permissions](https://docs.speechmatics.com/administration/management-tokens)
- [Projects: keys per project and shared billing](https://docs.speechmatics.com/administration/projects)
- [Authentication and regional endpoints](https://docs.speechmatics.com/get-started/authentication)

The documented Billing uses 1 credit = 1 USD. The documented grant for accounts
prior to 2026-08-01 is 25 USD, and for new registrations 100 USD. These values
do not allow inferring the balance of this account; its Billing was not consulted.

## Internal portal API: additional discovery

The portal's public assets confirm a way to query credits:

- [Observed route manifest](https://portal.speechmatics.com/assets/manifest-3686e8af.js)
  declares `routes/_authenticated.api.credits`, with path `api/credits` and a loader
  on the server. The Billing page has path `/settings/workspace/billing`.
- [Observed useCredits hook](https://portal.speechmatics.com/assets/useCredits-B4mjfwb_.js)
  calls `/api/credits` with the React Router fetcher and reads `data.credits`.
  The initial value comes from `metronomeCredits` in the authenticated route's loader.
  This identifies the name of the fields used by the client, not the exact
  unit/type of the response, which has not yet been obtained authenticated.
- The hook refreshes the balance on regaining focus/visibility and, after a refresh
  event, at 5, 30, 60 and 120 seconds. It is not an instantaneous streaming counter.
- The Billing public modules show `credits available`, payments and
  subscriptions. The server loaders are not present in these modules,
  so it was not possible to identify the upstream billing call.
- GET `/api/credits` test with `Accept: application/json`, without credentials: 302
  to `/login`, empty body.
- The same GET with the provided key in `Authorization: Bearer`: exactly
  the same 302. On this route, the transcription API key does not replace the login.
- The available browser also showed the login page. It was opened for
  authentication by the user; no session was obtained or exported.

Concrete next step: after logging in, open `/api/credits` in the same browser
and check the visible response, the balance, the unit and the workspace. If the session
authorizes that query, there is an internal way to read the balance. The integration into a
service would need supported session authentication and renewal; the discovery
does not demonstrate a stable API with an API key or management token. Do not store session
cookies in Git nor ask for credentials/cookies through the chat.
