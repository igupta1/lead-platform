# MSP Lead Magnet Pipeline

Python pipeline that scrapes signals about SMBs (jobs, funding, breaches),
scores them against three MSP niches (`it_msp`, `mssp`, `cloud`), and writes
a JSON blob consumed by the React pages at `/it-msps`, `/mssps`, `/cloud`.

## Setup

```bash
cd pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in real keys
```

## Run

```bash
source .venv/bin/activate
python -m msp_pipeline.daily_run
```

## Tests

```bash
source .venv/bin/activate
pytest
```

See `CLAUDE.md` for full conventions.
