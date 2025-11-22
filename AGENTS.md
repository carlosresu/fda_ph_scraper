# AGENT INSTRUCTIONS (FDA PH Scraper)

These rules are for GPT agents working inside `dependencies/fda_ph_scraper`. Follow them whenever you touch this submodule.

1. **Polars first, always.** Default to `polars` for every tabular task. Do not add new pandas/arrow/duckdb paths; if you must, note `TODO(polars): convert` so the migration backlog is clear.
2. **Stay current on Polars.** When editing `requirements.txt`, `install_requirements.py`, or setup code, bump `polars` to the latest stable release (and adjust companions like `pyarrow` if needed).
3. **Progressive Polars migration.** Prefer Polars-native transforms (lazy, streaming, vectorized ops) over Python loops. When you can’t feasibly convert a piece yet, leave a short `TODO(polars): convert` marker with the planned steps.
4. **Keep scraper helpers aligned.** Keep `text_utils.py`, `routes_forms.py`, and other shared helpers in sync with the latest algorithmic choices in `pipelines/drugs` (including `pipelines/drugs/scripts/`). Update `requirements.txt` alongside those changes so the standalone scraper keeps working.
5. **Preserve standalone runs/repo.** `food_scraper.py` and `drug_scraper.py` must keep running directly from this folder—whether or not it lives inside the eSOA repo—writing outputs under `output/` (with raw artifacts under `raw/`). Downstream callers can mirror those files into `inputs/drugs/`; do not move the primary outputs elsewhere.
