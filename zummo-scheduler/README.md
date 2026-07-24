# Zummo Bike — Weekly Scheduling Bot

An SMS-driven bot that replaces Steve's Tuesday-night, hand-built staff
schedule. It texts employees for availability, parses the replies, builds a
draft, lets Steve approve or request changes by text, then finalizes, PDFs, and
texts the schedule to everyone.

## Tech stack

- **Python 3.11 + FastAPI** — one language end to end (you're comfortable in
  Python), clean async webhook handling.
- **SQLite + SQLAlchemy** — lightweight persistent store, easy to eyeball.
- **APScheduler** — the weekly cron (configurable day/time).
- **Twilio** — outbound SMS + inbound webhook.
- **ReportLab** — the schedule PDF.
- **Anthropic (Claude Haiku)** — the *only* LLM usage, in exactly two places
  (parsing loose availability texts, parsing manager change requests).

## ⚠️ Read these before going live

1. **LLM billing.** The LLM calls are **metered pay-per-token** Anthropic API
   usage — a Claude/ChatGPT *subscription does not cover programmatic API
   calls*. At ~10–20 employees and ~2 calls/week the cost is a few cents/week,
   but it is real. This is called out because the original spec assumed
   "within an existing subscription," which isn't possible as built.
2. **Twilio trial limits.** On a free trial you can only text numbers you've
   *verified* in the Twilio console, and messages carry a trial prefix. Fine
   for testing; add a balance before texting real staff.
3. **A2P 10DLC.** US carriers require businesses to register automated texting
   (a one-time Twilio Console form, a few days to clear) before volume sending
   is reliable. Not needed for local testing; needed before launch.
4. **Public URL for inbound.** Twilio pushes replies to a webhook, so the app
   needs a public HTTPS URL (use `ngrok` locally, or your host's URL).
5. **PDF over SMS.** Plain SMS can't attach a PDF; MMS can carry a *media URL*.
   The app serves the PDF at `/files/<name>` and passes that as the MMS media
   URL when `PUBLIC_BASE_URL` is set. Without it, employees get the schedule as
   readable text only.
6. **Admin endpoints are unauthenticated** for local dev. Add auth before
   exposing publicly.

Anywhere the code makes an assumption (US phone numbers, day-only shifts,
placeholder staffing numbers, ConnecTeam's unknown API shape) it's marked with
a comment or a `TODO`.

## Setup

```bash
cd zummo-scheduler
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env with your values
python -m scripts.init_db          # create tables + seed staffing config
python -m scripts.seed_test_data   # fake employees (incl. 1 inactive)
```

### Environment variables

All config lives in `.env` (see `.env.example` for the full annotated list).
The essentials:

| Var | What it's for |
|-----|---------------|
| `DATABASE_URL` | SQLite path (default `sqlite:///./zummo.db`) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | Twilio creds + the shop's bot number |
| `TWILIO_TEST_MODE` | `true` skips webhook signature checks (for test creds) |
| `MANAGER_PHONE_NUMBER` | Where drafts go / approvals come from (Steve) |
| `ANTHROPIC_API_KEY` / `LLM_MODEL` | LLM parsing |
| `CYCLE_KICKOFF_*` / `TIMEZONE` | When the weekly cron fires |
| `REMINDER_OFFSET_HOURS` / `RESPONSE_CUTOFF_HOURS` | Reminder + no-response timing |
| `DEFAULT_OFFER_COUNT` / `OFFER_TIMEOUT_HOURS` / `MAX_OFFER_ATTEMPTS` | Revision offer loop |
| `CONNECTEAM_API_KEY` / `CONNECTEAM_BASE_URL` | Leave blank → stub adapter |
| `PUBLIC_BASE_URL` | Public URL for MMS PDF links |

## Running

```bash
uvicorn app.main:app --reload
```

Starting the app also starts the APScheduler background jobs:
- **Weekly kickoff** at the configured day/time → starts a cycle.
- **Reminder sweep** (hourly) → nudges non-responders past the offset.
- **Cutoff check** (hourly) → after the cutoff, marks no-responders and
  auto-builds the draft.
- **Offer timeout** (every 15 min) → expires stale shift offers and cascades.

For Twilio to reach the inbound webhook, expose the app publicly and point the
number's "A message comes in" webhook at `https://<your-url>/webhook/sms`.

## Web dashboard

Open `http://localhost:8000/` for a light web UI over the same services the SMS
flow uses (no second source of truth, no extra scheduling logic):

- **Dashboard (`/`)** — current cycle status, availability submissions (with
  `needs_review`/`no_response` flags), the draft grid, pending shift offers, and
  a failed-SMS warning. When a draft is awaiting approval it shows **manager
  review controls**: an Approve button and a structured "More/Fewer people on
  &lt;day&gt;" revision form. Web revisions use buttons, so they're fully
  deterministic — **no LLM call** to interpret a click (the LLM is only used to
  interpret Steve's free-text *SMS* replies).
- **Employees (`/employees`)** — list/add employees and toggle active status.
  Handy while ConnecTeam is stubbed; inactive employees are never texted or
  scheduled.

The dashboard is a convenience layer — Steve can still do everything by text.
⚠️ It's unauthenticated for local dev; add auth before exposing it publicly.

## Continuous integration

`.github/workflows/ci.yml` runs the test suite on every push/PR touching
`zummo-scheduler/`. Tests mock Twilio and the LLM, so CI hits no network and
costs nothing.

## Testing the full flow WITHOUT waiting a week

Every step has an admin endpoint, and `/admin/simulate-inbound` lets you fake
texts without Twilio at all. A complete dry run:

```bash
# 1. Start a cycle now (syncs employees, "texts" everyone for availability)
curl -X POST localhost:8000/admin/trigger-cycle

# 2. Simulate employees replying (matched by phone number)
curl -X POST "localhost:8000/admin/simulate-inbound?from_number=%2B15005550101&body=Mon%20Wed%20Fri"
curl -X POST "localhost:8000/admin/simulate-inbound?from_number=%2B15005550102&body=I%20can%20do%20weekends"

# 3. Build the draft (parses replies in one LLM call, texts Steve)
curl -X POST localhost:8000/admin/build-draft

# 4. Steve replies — approve, or request a change:
curl -X POST "localhost:8000/admin/simulate-inbound?from_number=%2B15551234567&body=need%20one%20more%20Thursday"
#    → the fairest available person gets a YES/NO offer text.

# 5. That employee accepts:
curl -X POST "localhost:8000/admin/simulate-inbound?from_number=%2B15005550104&body=yes"

# 6. Steve approves → finalize, PDF, text everyone:
curl -X POST "localhost:8000/admin/simulate-inbound?from_number=%2B15551234567&body=approved"

# Inspect state at any point:
curl localhost:8000/admin/status | python -m json.tool
```

> Use `%2B` for the `+` in phone numbers when passing them as query params.

## The weekly cycle (state machine)

```
collecting → draft_built → awaiting_approval ⇄ revising → finalized → sent
```

1. **collecting** — availability requests sent; replies stored raw.
2. **draft_built** — one batched LLM call parses all replies; deterministic
   algorithm assigns shifts using per-day staffing + a fairness ranking.
3. **awaiting_approval** — Steve gets the draft as readable text.
4. **revising** — if Steve asks for changes:
   - *"more people Thursday"* → ranks employees who listed Thursday and haven't
     worked it recently, texts the top one a **YES/NO offer**, only assigns on
     acceptance, cascades to the next on decline/timeout, and escalates to
     Steve if nobody's available. Also nudges any remaining non-responders.
   - *"fewer people Sunday"* → drops the least-fair-to-keep and re-sends.
5. **finalized → sent** — PDF generated, texted to all active staff, pushed to
   the ConnecTeam stub.

## Error handling

| Situation | Behavior |
|-----------|----------|
| Employee never replies | One reminder (cooldown-limited), then marked `no_response`; excluded from auto-assignment but shown on the draft/PDF so Steve sees the gap. |
| Twilio send fails | Retried with backoff (2s/4s/8s), logged to `sms_log` as `failed`, surfaced in `/admin/status`. One bad number never aborts a batch. |
| LLM can't parse a reply | That employee marked `needs_review` (raw text kept), flagged to Steve — never silently dropped, and the batch doesn't crash. |
| LLM API errors entirely | Whole batch degrades to `needs_review`; no crash. |
| Text from unknown number | Logged as `unmatched`, no crash, no reply. |
| Manager reply unclear | Bot asks Steve to rephrase rather than guessing. |

## ConnecTeam integration (stub only)

Employee list source of truth is meant to be ConnecTeam, but there are no API
docs/creds yet, so it's **scaffolded, not guessed**:

- `app/integrations/connecteam/base.py` — the interface: `sync_employees()`,
  `push_final_schedule()`, plus a normalized `ExternalEmployee` shape.
- `app/integrations/connecteam/stub.py` — a working stand-in (reads local DB,
  logs what it *would* push), with `TODO(real)` markers where the real API
  calls go.
- `app/integrations/connecteam/__init__.py` — factory; swaps in the real
  adapter automatically once creds are set.

Inactive/"sabbatical" employees are filtered by the `active` flag at sync time,
so they never get texted or scheduled. To go live: implement
`RealConnecTeam(ConnecTeamAdapter)` and wire it into the factory — nothing else
in the app changes.

## Project layout

```
app/
  main.py            FastAPI app, scheduler startup, /files PDF serving
  config.py          env-var settings (nothing hardcoded)
  db.py              SQLAlchemy engine/session
  models.py          all tables (+ status constants)
  scheduler.py       APScheduler jobs (weekly cron + sweeps)
  routers/
    sms_webhook.py   Twilio inbound webhook (signature-verified)
    admin.py         manual trigger/status/simulate endpoints for testing
    dashboard.py     web dashboard + employees admin (reuses the services)
  templates/         Jinja2 HTML for the dashboard
  services/
    cycle_service.py         state machine + inbound message routing
    availability_service.py  request/record/parse availability
    scheduling_service.py    deterministic draft + fairness ranking (no LLM)
    offer_service.py         consent-based "need more people" revision loop
    llm_parser.py            the ONLY two LLM call sites
    pdf_service.py           ReportLab schedule PDF
    sms_service.py           Twilio send wrapper (retry/backoff/logging)
    reminder_service.py      reminders + non-responder handling
  integrations/connecteam/   swappable adapter (interface + stub)
  utils/phone.py             E.164 normalization
scripts/
  init_db.py         create tables + seed staffing config
  seed_test_data.py  fake employees for local testing
config/
  staffing_requirements.yaml  per-day min/max (placeholder numbers — tune)
tests/               deterministic + mocked-LLM tests (no real API calls)
```

## Tests

```bash
pytest
```

All tests are offline — the LLM and Twilio are mocked, so `pytest` costs
nothing and hits no network.
```
