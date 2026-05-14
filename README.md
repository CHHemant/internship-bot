# Internship Auto-Apply Bot

Multi-agent AI pipeline: one master resume → ATS-optimized resumes + cover letters → auto-submit to research internship portals worldwide.

## Architecture (12 agents)

```
User prefs + Master resume
        ↓
Job Discovery Agent   ← analytics feedback loop (blue dashed)
        ↓
JD Analyzer Agent
        ↓        ↓  (parallel)
Resume      Cover Letter
Customizer  Agent
        ↓        ↓
    Verification Suite  ──(fail → retry up to 3×)──┐
        ↓ pass                                       │
Application Router                                   │
        ↓                                          JD Analyzer
Form Filler Agent                                    │
        ↓                                           ←┘
Submission Agent ── Error Handler
        ↓
Response Tracker
        ↓
Analytics Agent → feeds back to Job Discovery
```

## Folder structure

```
internship_bot/
├── agents/
│   ├── base_agent.py              # BaseAgent: LLM client, retry, error emit
│   ├── job_discovery/
│   │   └── agent.py               # Scrapes LinkedIn, DAAD, Euraxess, etc.
│   ├── jd_analyzer/
│   │   └── agent.py               # Extracts ATS keywords + country format rules
│   ├── resume_customizer/
│   │   └── agent.py               # Generates ATS-optimized custom resume
│   ├── cover_letter/
│   │   └── agent.py               # Generates culture-aware cover letter
│   ├── verifier/
│   │   ├── __init__.py            # VerificationSuite: orchestrates both layers
│   │   ├── ats_scorer.py          # ★ ATS scoring engine (deterministic NLP)
│   │   └── quality_reviewer.py    # LLM hallucination + tone checker
│   ├── router/
│   │   └── agent.py               # Maps listings → portal configs
│   ├── form_filler/
│   │   └── agent.py               # Playwright browser automation
│   ├── submission/
│   │   └── agent.py               # Final submit + confirmation capture
│   ├── tracker/
│   │   └── agent.py               # IMAP monitor + portal status polling
│   └── analytics/
│       └── agent.py               # Outcome analysis + preference weight updates
├── orchestrator/
│   └── pipeline.py                # Wires all agents for one listing
├── models/
│   └── schemas.py                 # All Pydantic schemas (single source of truth)
├── infra/
│   ├── redis/                     # Redis config + error bus
│   └── db/                        # SQLAlchemy models + Alembic migrations
├── config/
│   └── settings.py                # Pydantic-settings config from .env
├── tests/
│   ├── unit/
│   │   └── test_ats_scorer.py     # ★ 15 tests for ATS scoring logic
│   └── integration/
├── scripts/
│   └── run_pipeline.py            # CLI entrypoint
├── requirements.txt
└── .env.example
```

## Quickstart

```bash
# 1. Clone and install
git clone ...
cd internship_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Download spaCy model
python -m spacy download en_core_web_sm

# 3. Install Playwright browsers
playwright install chromium

# 4. Configure
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY at minimum

# 5. Start Redis (Docker)
docker run -d -p 6379:6379 redis:7-alpine

# 6. Run tests
pytest tests/unit/ -v

# 7. Run pipeline (dry run — no real submissions)
python scripts/run_pipeline.py --resume resume.pdf --prefs prefs.json
```

## ATS Scoring — how it works

The `ATSScorer` is deterministic (no LLM). It runs in Layer 1 of verification:

```
Composite score = required_coverage × 60
               + preferred_coverage × 25
               + format_score/100   × 15

Pass threshold: ≥ 70
```

Keyword matching (4 strategies, in order):
1. **Exact** — "python" in resume text → score 1.0
2. **Synonym** — "torch" matches "pytorch" → score 0.85
3. **Stemmed** — "researching" matches "research" → score 0.90
4. **Fuzzy** — RapidFuzz token_set_ratio ≥ 85 → score 0.75

Format deductions:
- Page count over limit → –20 pts
- Tables/columns detected → –15 pts
- Missing section headers → –5 pts each
- No email → –10 pts
- Europass required but absent → –15 pts

On fail: improvement notes go back to Resume Customizer. Max 3 retries → human queue.

## DRY_RUN mode

`DRY_RUN=true` (default) skips all actual form submission. Everything runs end-to-end except the final `submit` click and IMAP monitoring. Safe to run without fear of accidental applications.

## Adding a new country/portal

1. Add `CountryFormatRules` entry to `COUNTRY_FORMAT_MAP` in `jd_analyzer/agent.py`
2. Add portal automation in `form_filler/agent.py` (Playwright selectors)
3. Add routing logic in `router/agent.py`
4. Update `Country` enum in `models/schemas.py`

## Environment requirements

- Python 3.11+
- Redis 7+
- PostgreSQL 15+
- Chromium (via Playwright)
- ANTHROPIC_API_KEY (claude-sonnet-4-20250514)
