# Insurance lead magnet pipeline

Sibling of `pipeline/msp_pipeline/`. **No shared imports** — each
package owns its own DB schema, scoring weights, enrichment prompt,
Apollo title list, and JSON output shape. Two leads happening to
exist in both pipelines is allowed; the pipelines never read each
other's data.

See `pipeline/INSURANCE_PLAN.md` for the v2 plan: source strategy,
success criteria, build order, open questions.

## Setup

```bash
cd insurance_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in real keys
```

You can install both pipelines in the same venv if you prefer:

```bash
cd /path/to/repo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./pipeline ./insurance_pipeline
```

## Run

```bash
source .venv/bin/activate
python -m insurance_pipeline.daily_run
```

## Tests

```bash
source .venv/bin/activate
pytest
```

## Output

Writes `data/leads.json` with the shape `{generated_at, leads: [...]}`.
Uploaded to Vercel Blob key `insurance-leads-current.json` (separate
from the MSP pipeline's `leads-current.json`). Served by
`api/generate-insurance-leads.js` to the `/insurance` page.
