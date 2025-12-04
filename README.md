# FDA Philippines catalog scrapers (PIDS DRG)

Tools to pull the FDA Philippines drug and food catalogs, normalize them, and emit CSVs consumed by the PIDS DRG pipelines. Raw downloads stay inside this submodule so runs are reproducible.

## What’s here
- `drug_scraper.py` – downloads the FDA CSV export for human drugs, extracts the “as of” date, normalizes column names, and builds a brand → generic map. If DrugBank lean exports (`generics_lean.csv`, `synonyms_lean.csv`) are present in `../inputs/drugs/` (from the DrugBank submodule) it uses them to correct flipped brand/generic pairs.
- `food_scraper.py` – pulls the FDA food products catalog. Tries the official CSV export first; optional HTML pagination scraper fallback with deduping and cache reuse.
- `input/unified_constants.py` – shared token/normalization lists used during matching.
- `raw/` + `output/` – created on demand; hold cached downloads and normalized CSVs.

## Setup
Python 3.10+ with `requests`, `pandas`, `pyarrow`; `ahocorasick` is optional but speeds up brand/generic detection.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pandas ahocorasick
```

## Usage
Run scripts from anywhere; they resolve paths relative to this folder.

```bash
# Drug catalog brand → generic map
python drug_scraper.py --outdir output
# Food catalog; enable HTML fallback if the export endpoint is down
python food_scraper.py --outdir output --allow-scrape
```

Key flags:
- `--outfile` (both) to override the default dated filename.
- `--force` (food) to ignore existing outputs and re-download.
- `--allow-scrape` (food) to page through the site when the CSV export is incomplete.

Each run writes under `./output/` and copies drug outputs into `../inputs/drugs/` when that directory exists so downstream pipelines can pick them up.

## Notes
- The drug scraper caches raw FDA exports in `./raw/` keyed by date and reuses them when newer than the site’s advertised date.
- Be gentle with the FDA site—avoid rapid retries and keep scraping intervals reasonable.
