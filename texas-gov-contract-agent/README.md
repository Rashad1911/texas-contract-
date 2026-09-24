# Texas Government Contract Opportunity Agent

Finds government contracts in **Dallas–Fort Worth, Houston, Austin, and San Antonio** that fit a
low-startup, manage-and-subcontract business model — then researches them so you only read the
strongest 3–5.

Every 3 days it:

1. **Searches** SAM.gov (VA, DoD, Army, Air Force, Navy, GSA, DHS, USPS, and every other federal agency)
   plus the official bidding portals for the City of Dallas, Dallas County, City of Fort Worth, City of
   Houston, Harris County, City of Austin, Travis County, City of San Antonio, Bexar County, the State
   of Texas (ESBD), and the BuyBoard, TIPS, and Choice Partners purchasing cooperatives. It also tracks
   **IDIQs, MATOCs, BPAs, and task orders** — who holds them and when they're recompeted (section 11).
2. **Filters out** poor fits and writes down why (wrong set-aside, goods not services, major construction,
   outside your markets, too little time, huge workforce…).
3. **Researches** the survivors with nine agents: requirements (must have / nice to have / potential
   problems), subcontracting rules, who won last time, bid strategy, proposal drafts, and a red team that
   tries to kill each one.
4. **Scores and ranks** them: 🔥 Top · 🟡 Worth reviewing · ⚪ Watchlist · ❌ Filtered.
5. **Emails you** a short phone-friendly report (2–5 minutes) — only when something new or changed.
6. **Publishes a dashboard** you can search from your phone and share with anyone.

Your LLC is pre-filled in `config/settings.yaml` (Deyampert & Patterson Enterprises LLC, D&P Cleaning).

> **Try it without any keys:** `python main.py demo` builds a dashboard and two sample emails from made-up
> data into `build/demo/`. Open `build/demo/site/index.html` in a browser.

---

## 1. Install

You need Python 3.11+ (3.12 recommended) and a free GitHub account.

```bash
git clone https://github.com/<you>/texas-gov-contract-agent.git
cd texas-gov-contract-agent
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in your keys (section 2)
python -m pytest -q                # 28 offline tests, no keys needed
python main.py demo                # sample run → build/demo/
```

To put it on GitHub: create a new repository, then upload this folder (GitHub Desktop, or
`git init && git add . && git commit -m "first" && git remote add origin … && git push -u origin main`).

## 2. API keys

| Key | Needed? | Where to get it |
|---|---|---|
| `SAM_API_KEY` | Yes, for federal opportunities | Sign in at [SAM.gov](https://sam.gov), open your profile's **Account Details** page, and request a **Public API Key**. |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | Optional | Adds plain-English summaries and an extra red-team pass. Everything works without it. |
| Email (`EMAIL_USERNAME`, `EMAIL_PASSWORD`, `EMAIL_TO`) | For the email report | See section 8. |

**SAM.gov limits.** A key from a personal account with no role on a registered business gets **10 requests
a day** — the agent budgets for that (it searches Texas only, and keeps 2 requests in reserve). Once your
LLC's SAM.gov entity registration is active and your account has a role on it, keys get **1,000 a day**:
then set `sam.daily_request_limit: 1000` in `config/settings.yaml`. SAM.gov keys **expire periodically**
(your Account Details page shows the date). When they do, the email and dashboard say
"SAM.gov rejected the API key" — generate a new one and update the secret.

Federal awards also require an active SAM.gov entity registration (UEI). Registration is free and can take
several weeks, so start early. When it's active, set `company.sam_registered: true` and fill in your UEI.

## 3. GitHub Secrets

In your repository: **Settings → Secrets and variables → Actions → New repository secret.** Add:

- `SAM_API_KEY`
- `OPENAI_API_KEY` (optional) — or `ANTHROPIC_API_KEY`
- `EMAIL_USERNAME`, `EMAIL_PASSWORD`, `EMAIL_TO`
- optional: `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`

Optional **Variables** (same page, *Variables* tab): `SITE_URL` (if your dashboard isn't at the default
address) and `OPENAI_MODEL`.

Secrets never go in the code or config files. `.env` is for running on your own computer and is ignored by git.

## 4. Run it manually

**On GitHub:** Actions tab → **Contract Scout** → **Run workflow**. Leave "Run a full search now" checked.

**On your computer:**

```bash
python main.py run --force            # full search now (sends the email if configured)
python main.py run --force --no-email # research + dashboard only; email preview saved to output/
python main.py sources --check        # test every portal right now and print what worked
python main.py list                   # everything in the database, best first
```

## 5. Automatic runs every 3 days

`.github/workflows/contract-scout.yml` wakes up every day at 6 AM Central. The app does a full search only
when 3 days have passed since the last one (`schedule.run_every_days` in `config/settings.yaml`) and skips
quietly otherwise. After each run it commits the updated database (`data/`) and dashboard (`docs/`) back to
the repository.

Change the cadence with `run_every_days`. Change the wake-up time with the `cron` line (it's in UTC).
GitHub may pause scheduled workflows in repositories with no activity for 60 days; the agent's own
commits normally keep it active, but if runs stop, re-enable the workflow on the Actions tab.

If the "Save results" step fails with a permission error: **Settings → Actions → General → Workflow
permissions → Read and write permissions.**

## 6. Change cities and portals

**Markets** live in `config/locations.yaml`. Each market lists its cities, counties, ZIP prefixes, and
military/federal installations. To stop searching a market, set `enabled: false`. To add one, copy a
block (for example `el_paso:`) and fill in its cities, counties, and ZIP prefixes.

**Portals** live in `config/sources.yaml`. Every entry was checked on the entity's official site and has an
`info_url` (the official page that links to the portal) so you can fix it yourself if a portal moves. To
add a school district, transit authority, or airport, copy a block and set `market:` and `type:`
(`bonfire`, `html`, `rss`, `json_api`, `beacon`, or `manual`).

The first real run tells you which portals worked: the **Where we searched** table on the dashboard and a
line in the email. Local portals change their pages without notice, so expect to adjust one or two.

**Portals that need a login** (Travis County's BidNet, San Antonio's SAePS) are listed on the dashboard as
"Check by hand". Register on them (free) and check them when you get the email.

### Turning on the Texas ESBD feed (one-time, about 2 minutes)

The ESBD is a JavaScript app, so the agent needs the data address the page itself uses:

1. Open <https://www.txsmartbuy.gov/esbd> in Chrome on a computer.
2. Press F12 → **Network** tab → click **Fetch/XHR** → reload the page.
3. Click the request whose **Response** shows the list of solicitations (JSON).
4. Right-click it → **Copy → Copy URL**. Paste it as `api_url:` under `texas_esbd` in `config/sources.yaml`.
   If the request's method is POST, set `api_method: POST` and paste its request body under `api_body:`.

The same trick works for any other JavaScript portal.

## 7. Add services

Service lines live in `config/services.yaml`. Copy a block and change it:

```yaml
  irrigation:
    label: "Irrigation repair"
    keywords: [irrigation repair, sprinkler repair, irrigation maintenance]
    naics: ["561730"]
    startup: low        # low | medium | high
    outsource: high     # how naturally it fits a manage-and-subcontract model
    margin: medium
    licenses: ["Texas licensed irrigator (TCEQ)"]
    subcontractor: "Licensed irrigation contractor"
    related: [landscaping]
```

Keywords that appear in a solicitation's **title** count most. `exclude:` keeps a category from grabbing
specialty trades (for example, elevator work is excluded from general facility maintenance).

**Discovery.** The Discovery agent watches service contracts that match none of your categories. When the
same kind of work shows up in two or more solicitations, it adds that category to
`data/discovered_services.yaml` and searches for it next run (turn this off with `discovery.auto_add: false`).
To reject one, set `approved: false` and `rejected: true` on it; to keep one permanently, copy it into
`config/services.yaml`.

Also keep these accurate in `config/settings.yaml` — the filter and red team use them:
`certifications` (set-asides you can bid), `existing_capabilities`, `years_in_business`,
`insurance_on_hand`, and `bonding_capacity`.

## 8. Change the email

- **Who gets it:** the `EMAIL_TO` secret. Separate several addresses with commas.
- **Gmail:** turn on 2-Step Verification, then create an **App Password** (Google Account → Security →
  App passwords) and use it as `EMAIL_PASSWORD`. Your normal password won't work.
- **Outlook / Microsoft 365 or another provider:** set `SMTP_HOST` and `SMTP_PORT` secrets
  (for example `smtp.office365.com` and `587`). Any SMTP relay works (SendGrid, Mailgun, Amazon SES…).
- **Subject, size, empty cycles:** `email:` and `limits:` in `config/settings.yaml`
  (`top_in_email: 5`, `review_in_email: 3`, `send_when_empty: false`).
- **Test it:** `python main.py test-email`, or look at `output/last_email.html` after any run.

The email only includes opportunities you haven't been sent yet, plus **🔔 Updates** when something you're
tracking changes (deadline moved, new addendum, set-aside changed). Items already sent appear as one line
under "Still open". No email goes out when nothing meaningful happened.

## 9. Mark opportunities PURSUE, WATCH, or PASS

Every opportunity has a code like `TX-1A2B3C` (in the email and on the dashboard).

**From your phone (owner mode):** open your dashboard link once with `?owner=1` added to the end
(`https://<you>.github.io/<repo>/?owner=1`). That phone now shows **Pursue / Watch / Pass** buttons on each
opportunity. Tapping one opens a pre-filled GitHub issue; submit it and the *Record decision* workflow saves
it, rebuilds the dashboard, and closes the issue. Only issues **you** (the repository owner) open are
processed, so friends can't change your decisions. `?owner=0` turns owner mode off on a device.

**From a computer:** `python main.py mark TX-1A2B3C pursue --reason "have two subs lined up"`

**From GitHub:** Actions → **Record decision** → Run workflow.

**Or edit `data/decisions.yaml`** directly.

What each decision does:

- **PURSUE** — writes the full proposal kit (below) and sends a 🔔 update whenever the solicitation changes.
- **WATCH** — keeps monitoring; you get a 🔔 update if the deadline, attachments, or set-aside change.
- **PASS** — records your reason and stops emailing about it.

## 10. Review the detailed analysis

**Dashboard.** Tap any opportunity for the full record: why it made the list, biggest risk, what to do
next (in order), *here is what happened last time* (prior winner and award), requirements with the exact
text they came from, whether subcontracting is allowed (and the model: Agency → your LLC → subcontractor),
the ten-part bid strategy, the red team's findings, the 15-factor score breakdown, and official links.

**Proposal kit.** For PURSUE items and top opportunities, the Proposal agent drafts (worth-reviewing items get the bid/no-bid analysis and the questions email): capability statement,
bid/no-bid analysis, proposal outline, scope-of-work response, management plan, staffing plan,
subcontractor scope, pricing worksheet (CSV with formulas), questions for the contracting officer (an email
signed "Rashad"), proposal checklist, and a vendor/subcontractor agreement outline. They appear on the
dashboard with a copy button, and `python main.py proposal TX-1A2B3C` writes them to
`output/proposals/TX-1A2B3C/`. On GitHub, each run's drafts and email preview are saved as a downloadable
artifact on the run's page for 30 days.

**Terminal.** `python main.py show TX-1A2B3C` (add `--json` for everything).

## 11. Task orders, IDIQs, and other contract vehicles

A lot of government work flows through **contract vehicles** instead of one-off bids: IDIQs (including
multiple-award MATOCs and single-award SATOCs), blanket purchase agreements (BPAs), local term and
as-needed contracts, job order contracts, and purchasing co-ops. The agent handles them in four ways.

**1. It reads vehicles correctly.** When a solicitation is an IDIQ or BPA, the dashboard and email show the
contract type and an honest value, for example *"Ceiling $24.0M shared by up to 5 holders (not guaranteed);
$2,500 guaranteed."* The ceiling is the most the government *might* order across every holder over
several years — it's never counted as revenue. The red team adds what's different about vehicles (a seat
isn't guaranteed work; each order is competed again), and the bid strategy adds the questions to ask
(how many awards, the minimum guarantee, expected order volume, on-ramps).

**2. It explains task orders you can't answer.** Orders under an *existing* IDIQ or BPA are competed only
among that vehicle's holders ([FAR 16.505, "fair opportunity"](https://www.acquisition.gov/far/16.505)), so a
publicly posted task order request is usually closed to everyone else. Those are filtered with that reason.
If you hold a vehicle, list it under `company.vehicles_held` in `config/settings.yaml` and its task orders
come through.

**3. It finds the companies that hold vehicles (teaming leads).** Most task orders are never posted
publicly at all. The way in is to subcontract for a holder. The **Contract vehicles and teaming leads** table
on the dashboard lists them, from two official sources:

- SAM.gov award notices for IDIQs and BPAs in Texas (these ride along in the same SAM.gov request, so they
  don't use up your 10-a-day limit).
- USAspending.gov: every active IDIQ and BPA with a Texas place of performance for the NAICS codes in
  `vehicles.idv_naics` (free, no key).

**4. It warns you about recompetes early.** When an existing vehicle stops taking orders within 18 months
(`vehicles.recompete_window_months`), it appears on your ⚪ Watchlist as "Recompete expected", with the
current holder and dollars spent so far. That's the time to introduce yourself to the contracting office.

**Regional IDIQs** often list several states or none. Beyond the Texas search, the agent runs nationwide
SAM.gov searches for your core NAICS codes (`vehicles.sam_naics_queries`) and keeps only records that involve
Texas. These extra searches never eat into the requests reserved for reading full solicitation text; with
the 1,000-a-day limit (after your entity registration) they all run every time.

**Texas purchasing co-ops** are the local-government version of an IDIQ: one award lets school districts,
cities, and counties across Texas buy from you without their own bid. BuyBoard's proposal invitations are read
automatically. TIPS and Choice Partners show as "Check by hand" — register as a vendor on each to get their
solicitation notices.

**What it can't see:** GSA eBuy (where many GSA Schedule task orders are posted) is visible only to Schedule
holders, and agency-internal task order competitions aren't public. Holding a GSA Multiple Award Schedule
contract is a longer-term move that opens eBuy; your local APEX Accelerator can walk you through it.

---

## Your dashboard and sharing it

**GitHub Pages (free):** Settings → Pages → *Build and deployment* → Source: **Deploy from a branch** →
Branch **main**, folder **/docs** → Save. Your link is `https://<you>.github.io/<repo>/`. Share it with anyone;
it's read-only. Search works across titles, agencies, cities, services, and solicitation numbers.
A sample version lives at `…/demo/`.

**Privacy.** On GitHub's free plan, the dashboard needs a **public** repository, so anyone who looks can see
the code, the database, your decisions, and proposal drafts. The dashboard link itself isn't listed anywhere,
and the page asks search engines not to index it (`site.allow_search_engines`). Only information that's already
public (company name, website) is in the config — never add your EIN or bank details.

A private repository hides the code and data, but the dashboard page is still viewable by anyone who has its
link. That's true on paid GitHub plans and on Netlify's free plan too (set `NETLIFY_AUTH_TOKEN` and
`NETLIFY_SITE_ID` secrets and the workflows deploy `docs/` there). A login-protected page needs a service with
access control, such as Cloudflare Access. To hide decisions or drafts from anyone who opens the dashboard,
set `site.show_decisions` / `site.include_drafts` to `false`.

## How the scoring works

Fifteen factors, each 0–10, weighted in `config/settings.yaml`: easy to start, capital needed, can be
subcontracted, contract size, competition, experience bar, licensing, insurance, bonding, time to prepare,
location, simplicity, margin, past award data, and realism for a small LLC. Each unresolved **major** risk
from the red team costs extra points; a **kill** finding caps the score.

A **Top opportunity** also has to clear four rules, so a thin listing can't sneak onto the list: at most one
major risk, the actual solicitation text was available (not a one-line listing), 5+ days to prepare, and
subcontracting confirmed — unless it's work you already do yourself. When a rule holds an item back, the
dashboard and email say which one.

**The score is fit, not the odds of winning.**

## Important limits (read these)

- **Subcontracting is never assumed.** On federal small-business set-asides for services, FAR 52.219-14
  generally limits what you can pay to subcontractors that aren't "similarly situated" small businesses to
  50% of the amount the government pays you — a pure pass-through model usually won't comply. Local and
  state contracts are often friendlier (M/WBE, SBE, and HUB subcontracting goals). The agent labels each
  opportunity SUPPORTED, APPROVAL REQUIRED, LIMITED, RESTRICTED, or UNKNOWN from the actual text.
- **Everything is a research aid.** Requirements come from the text the scanner could read. Always confirm in
  the official solicitation. Have pricing reviewed by a CPA and contracts by an attorney; your local
  **APEX Accelerator** offers free government-contracting counseling.
- **Local portals change.** The connectors were written against each portal's public pages but couldn't be
  tested live from the build environment. Your first run's *Where we searched* table shows what works; fix a
  broken one by updating its URL in `config/sources.yaml`.
- **The FAR is being rewritten** (the Revolutionary FAR Overhaul that began in 2025). Always use the clause
  text actually included in the solicitation you're bidding.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "SAM_API_KEY is not set" | Add the secret (section 3). |
| "SAM.gov rejected the API key" | The key expired. Generate a new one (section 2). |
| "SAM.gov daily request limit reached" | Wait for the reset (midnight UTC), or avoid several manual runs on the same day. |
| A local source shows "Couldn't read" or "No open items" | Open its `info_url`, find the current listing page, and update `url` in `config/sources.yaml`. |
| No email arrives | Check the three email secrets; Gmail needs an App Password. Look at the workflow run's log and the `report-…` artifact. |
| Dashboard link shows 404 | Turn on GitHub Pages (main, /docs) and run the workflow once. |
| Decision buttons don't appear | Open the dashboard once with `?owner=1` on that device. |

## Project layout

```
agents/          the nine agents: scout, filter, award analyst, requirements, subcontracting,
                 bid strategy, proposal, red team, discovery (+ scoring)
connectors/      SAM.gov, USAspending (awards + IDIQ/BPA vehicles), Bonfire, Beacon, HTML/RSS/JSON portals, per-market builders
                 (dallas.py, houston.py, austin.py, san_antonio.py, texas.py), attachment reader
config/          settings, locations, services, sources (all plain YAML)
core/            config loader, opportunity record, text/date/money extraction, contract-vehicle detection,
                 HTTP, LLM wrapper
database/        SQLAlchemy models + repository (SQLite at data/contracts.db, or DATABASE_URL)
reports/         email report, chart, dashboard builder and template
data/            database, decisions.yaml, discovered services, sample data for the demo
docs/            the published dashboard (rebuilt every run) and docs/demo/
tests/           offline tests
main.py          command line: run, demo, mark, list, show, proposal, build-site, test-email, sources
```
