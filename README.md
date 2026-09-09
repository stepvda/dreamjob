# Dream Job

**AI-assisted job discovery and application platform.**

Most job search tools react to what has been published. Dream Job starts from
who you are and what you actually want, maps the relevant slice of the market —
*including companies that have not advertised* — judges each company's capacity
and likelihood to hire, and then removes the friction of applying.

---

## What it does

```
  PROFILE ──────→ PLAN ──────→ DISCOVER ──────→ APPLY ──────→ FOLLOW UP

  LinkedIn + CV   Structured    Company         Hiring         Responses
  merged, and     directives    profiles and    contacts,      recorded,
  the job you     bound the     five-year       validated,     patterns
  actually want   search        financials      then written   found
```

- **Builds a profile you can trust.** Your LinkedIn export and CV are merged,
  and every disagreement between them is surfaced for you to resolve rather than
  silently decided. Your photo comes across for the tailored CV.
- **Takes your dream job seriously.** A free-text statement, in your own words,
  becomes a structured model — target roles, deal-breakers, values — that drives
  matching, discovery and scoring.
- **Finds unadvertised openings.** For companies with no matching vacancy, it
  reasons from their finances, hiring signals, department map and competitors'
  hiring to propose roles they are likely to need. These are labelled
  everywhere, and the email never claims a vacancy exists.
- **Reads the accounts.** Five years of filings per company, turned into two
  scores — ability to pay and investment capacity — each justified with the
  actual figures.
- **Writes the application.** Tailored CV, a company briefing and a motivation
  document for you, and an introduction email for them. A factual-consistency
  check blocks anything the profile does not support.
- **Learns what works.** Record what came back — including by phone or LinkedIn —
  and it tells you which kinds of role and company actually reply, with the
  sample sizes, and where to redirect.

**Nothing is sent without you approving it.** Scoring is advisory throughout.

---

## Running it

**Requirements:** Python 3.11+, Node 18+, and a DeepSeek API key.

```bash
git clone <this repo> && cd dreamjob

cp .env.example .env
python3 -m dreamjob.security.crypto --generate-key   # paste both values into .env
$EDITOR .env                                          # add DEEPSEEK_API_KEY

pip install -r requirements.txt
cd frontend && npm install && cd ..

./scripts/dev.sh
```

| | |
|---|---|
| Application | **http://127.0.0.1:5173** |
| API | http://127.0.0.1:8000 |
| API reference | http://127.0.0.1:8000/docs |

Create an account on first run. Migrations apply automatically at startup.

### Optional, when you need them

**Browser automation** — for LinkedIn and Glassdoor, which block scrapers. You
launch your own browser and log in; the system attaches to it and never sees
your credentials.

```bash
./scripts/browser.sh          # dedicated profile, remote debugging port
```

> ⚠️ LinkedIn's user agreement prohibits automated access **including through a
> session you logged into yourself**. Your account may be restricted. Dream Job
> shows this warning before the first run and refuses to proceed until you
> acknowledge it. The decision is yours to make knowingly.

**Sending mail** — either backend:

- *Gmail OAuth* — set `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then connect on
  the Mail setup screen. Replies land in your own inbox and are detected.
- *Resend* — set `RESEND_API_KEY`. Sends from your own domain; no inbox, so
  replies are recorded by webhook or entered by hand.

**A local model** — for the privacy-sensitive steps (composite profile, CV,
motivation document), any OpenAI-compatible endpoint:

```bash
DREAMJOB_LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
DREAMJOB_LOCAL_LLM_MODEL=llama3.1
DREAMJOB_LOCAL_LLM_TASKS=profile,cv,motivation
```

---

## Using it

The **Where I am** screen is the map. Fifteen stages across five phases, each
showing what is done, what is running, and — when something is blocked — *what
would unblock it*. Every stage links to the screen that advances it.

Press **`?`** on any screen for its instructions, or click the **?** beside any
label for that concept. 18 screens documented, 22 glossary terms, 386 inline
tips: the help is part of the product, not an afterthought.

A first pass, roughly in order:

1. **Profile** — upload your LinkedIn PDF export and your CV, resolve the
   conflicts, mark anything you never want disclosed.
2. **Dream job** — write it properly. It is the most influential thing you enter.
3. **Composite profile** — confirm or reject the online findings. Anything not
   matched to you with high confidence waits for your decision.
4. **Directives** — bound the search: titles, company type, location, work
   arrangement. Turn on discretion mode if you are currently employed.
5. **Campaigns** — review the plan, its cost and what will be reused, then launch.
6. **Opportunities** — the ranked list. Pin, re-order, discard. Your order wins.
7. **Applications** — read what was written before approving it.
8. **Responses** and **What works** — record what comes back, including
   rejections. That is what the advice is built from.

---

## Architecture

React SPA · Python/FastAPI · SQLite (WAL) · DeepSeek (OpenAI-compatible) ·
Playwright over CDP.

```
frontend/src            React SPA, 22 lazy-loaded screens
backend/dreamjob
  api/routers           18 routers, 301 routes
  pipeline              intake, enrichment, planning, collection, profiling,
                        financial, opportunities, scoring, contacts
  adapters              24 source adapters over one four-step contract
  documents             CV, briefing and motivation generation
  mail                  Gmail OAuth and Resend behind one interface
  postapp               pipeline board, replies, learning, mock interviews
  db/repositories       the only place SQL is written
  llm  egress  jobs  security   the shared services
```

The design rests on funnelling five concerns through exactly one module each,
which is what makes the corresponding requirements enforceable rather than
aspirational:

| Every… | goes through | so that |
|---|---|---|
| SQL statement | `db/repositories` | isolation holds and PostgreSQL stays possible |
| outbound request | `egress/client.py` | robots.txt, rate limits and caching are not optional |
| LLM call | `llm/client.py` | budget, injection defence, redaction and audit always apply |
| external source | `adapters/base.py` | a new job board needs no pipeline change |
| long-running task | `jobs/runner.py` | everything is pausable and resumable |

**Scale:** 172 Python modules / 72,820 lines · 113 JS modules / 34,082 lines ·
**528 tests** · 71 tables · 24 adapters · 21 versioned prompt templates.

---

## Development

```bash
PYTHONPATH=backend python3 -m pytest tests/ -q     # 528 tests
python3 -m ruff check backend/dreamjob             # lint
cd frontend && npm run build                       # production build
python3 scripts/traceability.py                    # requirement coverage
```

Tests run against a temporary migrated database with no network access.

**Adding a source** is one file: subclass `SourceAdapter`, implement
`plan / fetch / parse / normalise`, decorate with `@register_adapter`. It
appears in the catalogue at next boot.

---

## Documentation

| | |
|---|---|
| [Functional Design](docs/Functional_Design.md) | What the product does, screen by screen, and every judgement call |
| [Technical Architecture](docs/Technical_Architecture.md) | How it is built, and why it is built that way |
| [DPIA](docs/DPIA.md) | Data-protection impact assessment (NFR-304) |
| Word editions | `docs/Dream_Job_Functional_Design.docx`, `docs/Dream_Job_Technical_Architecture.docx` — illustrated |

**155 of 157 requirements (98%)** from the specification are implemented and
cited in the source: 103/104 Must, 43/44 Should, 9/9 Could. The two that are not
are a performance target that needs a real campaign to measure, and the DPIA
itself. Known limitations are listed in the Functional Design, §12 — they are
stated rather than glossed.

---

## Data protection

The product processes two kinds of personal data and treats them differently.

**Yours.** Encrypted at rest with a key derived for your account alone. Fields
you mark *do not disclose* are excluded from every generated document and
stripped before any prompt leaves the machine. Full export and complete erasure
are one call each — and erasure derives its table list from the live schema, so
a table added later cannot be silently missed.

**Hiring contacts'.** Only professional contact details, kept to what a
professional introduction needs. Every introduction email offers a way to
object, and **an objection blocks that address permanently, for everyone** on the
installation. Contacts gathered through browser automation expire with the
campaign that collected them and are never shared between job seekers.

DeepSeek processes data outside the EU. You are told before any profile data is
sent, and your consent is recorded (CR-410). Privacy-sensitive steps can be
routed to a local model instead.

---

## Licence and status

Built to the *Dream Job* Software Requirements Specification v0.3.
Not yet production-deployed; the DPIA must be completed and reviewed first
(NFR-304).
