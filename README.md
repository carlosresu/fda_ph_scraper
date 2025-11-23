# FDA PH Scraper

Collection of tools that scrape the FDA Philippines food and drug catalogs, normalise the output, and drop the resulting CSVs under this module’s own `output/` directory. Downstream automation (e.g., `run_all.py`) then mirrors those exports into `inputs/drugs/`. This folder is intended to become its own GitHub submodule (`carlosresu/fda_ph_scraper`) so it can be shared across projects; the current copy mirrors the content that would live inside that submodule.

## Layout

- `food_scraper.py` – paginated HTML scraper for the food catalog
- `drug_scraper.py` – CSV downloader that builds the brand→generic export
- `routes_forms.py`, `text_utils.py` – portable helpers consumed by the two scrapers
- `requirements.txt` / `install_requirements.py` – helper to bootstrap the Python dependencies

## Setup

The repo root installers (`install_requirements.ps1` on Windows, `install_requirements.sh` on macOS/Linux) will set up this scraper automatically as part of the global environment bootstrap. To install just this module’s dependencies manually:

```bash
cd /path/to/esoa/dependencies/fda_ph_scraper
python -m pip install -r requirements.txt
# or, equivalently
python install_requirements.py
```

## Usage

Files can be executed directly (the module automatically locates the repository root even when run from within the submodule) or via `python -m`:

```bash
python -m dependencies.fda_ph_scraper.food_scraper      # writes to dependencies/fda_ph_scraper/output/fda_food_products.csv
python -m dependencies.fda_ph_scraper.drug_scraper      # writes brand maps to dependencies/fda_ph_scraper/output/fda_brand_map_<date>.csv
```

Each scraper writes raw artifacts under `dependencies/fda_ph_scraper/raw/` so the HTTP downloads stay inside the submodule tree. To feed eSOA directly, copy whatever you need from the module’s `output/` folder into `../inputs/drugs/`.

## Publishing as a Submodule

1. Create the [https://github.com/carlosresu/fda_ph_scraper](https://github.com/carlosresu/fda_ph_scraper) repository.
2. Push the contents of this folder to that repo (`git subtree push` or similar) and tag a release.
3. Add the published repo as a submodule inside `dependencies/fda_ph_scraper` by running:
   ```bash
   git submodule add https://github.com/carlosresu/fda_ph_scraper dependencies/fda_ph_scraper
   git commit -m "Add FDA PH scraper submodule"
   ```
4. Update `.gitmodules` as needed and ensure `dependencies/fda_ph_scraper/install_requirements.py` continues to operate in the new location.

Sensitive artifacts (raw HTML, CSV outputs) are already excluded by `.gitignore`.
