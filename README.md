# vob-estimate-agent

Backend and chat agent for the patient VOB (verification of benefits) estimate app.

Flow: patient details are collected in chat while Stedi 270/271 runs in the background, the patient picks a
reason for visit from a locked menu, then a deterministic engine combines the parsed 271 with each clinic's
fee schedule to produce an out-of-pocket range per clinic, plus a side-by-side comparison. The LLM only
talks; it never computes prices.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add keys; STEDI_MODE=mock needs none for Stedi
.venv/bin/uvicorn vob_agent.api:app --reload    # chat UI at http://127.0.0.1:8000, API docs at /docs
.venv/bin/python -m pytest -q
```

Reference data (clinics, payers, bundles, CPT catalog, fee schedule) is read live from Google Sheets
(ids in `config/settings.json`, shared as "Anyone with the link: Viewer"). `tests/fixtures/*.csv` are synthetic test fixtures only.

## Layout
- `vob_agent/api.py` FastAPI app and chat endpoints
- `vob_agent/agent.py` LLM tool-calling loop (OpenRouter or Anthropic)
- `vob_agent/stedi.py`, `parser271.py` Stedi client and 271 parsing
- `vob_agent/engine.py` cost-share engine (copay, deductible, coinsurance, out-of-pocket max) and comparison
- `config/` menu and settings; `tests/` with synthetic fixtures in `tests/fixtures/`

## Status
Prototype. Use synthetic or consenting test patients only: no HIPAA agreements are in place with the LLM
provider or host. Live Stedi call verified against Cigna (62308); benefit-category mappings still need
validation against more real 271s.
