#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import re
import shutil
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import requests

MODULE_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = MODULE_ROOT / "scripts"

# Import shared utilities - prefer synced scripts from main esoa repo
import sys
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

try:
    # First try synced scripts (from main esoa repo)
    from scripts.text_utils import normalize_text
    from scripts.routes_forms import FORM_TO_ROUTE, parse_form_from_text
except ImportError:
    try:
        # Fall back to package import
        from .text_utils import normalize_text
        from .routes_forms import FORM_TO_ROUTE, parse_form_from_text
    except ImportError:
        # Fall back to standalone import (legacy)
        from text_utils import normalize_text
        from routes_forms import FORM_TO_ROUTE, parse_form_from_text

DEFAULT_OUTPUT_DIR = MODULE_ROOT / "output"
RAW_DIR = MODULE_ROOT / "raw"

BASE_URL = "https://verification.fda.gov.ph"
HUMAN_DRUGS_URL = f"{BASE_URL}/drug_productslist.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; eSOA-BrandMap/1.0; +https://github.com/)"
}


AS_OF_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"as of\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})", re.IGNORECASE),
    re.compile(r"as of\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.IGNORECASE),
    re.compile(r"as of\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE),
)

DATE_FILE_RX = re.compile(r"FDA_PH_DRUGS_(\d{4}-\d{2}-\d{2})\.csv$", re.IGNORECASE)


def _strip_ordinals(value: str) -> str:
    """Remove ordinal suffixes (1st→1, 2nd→2, …) for reliable datetime parsing."""
    return re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", value)


def _parse_date_candidates(value: str) -> Optional[date]:
    """Try multiple date formats until one succeeds; return None if all fail."""
    cleaned = _strip_ordinals(value.strip())
    for fmt in ("%B %d, %Y", "%B %d %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _extract_as_of_date(html_text: str) -> date:
    """Locate the "as of" date on the landing page and return it as a date object."""
    for pattern in AS_OF_PATTERNS:
        match = pattern.search(html_text)
        if match:
            parsed = _parse_date_candidates(match.group(1))
            if parsed:
                return parsed
    raise RuntimeError("Unable to determine the FDA PH drug catalog 'as of' date.")


def _existing_raw_dates(raw_dir: Path) -> List[Tuple[date, Path]]:
    """Collect already-downloaded raw CSVs and their associated dates."""
    items: List[Tuple[date, Path]] = []
    if not raw_dir.is_dir():
        return items
    for path in raw_dir.glob("FDA_PH_DRUGS_*.csv"):
        match = DATE_FILE_RX.search(path.name)
        if not match:
            continue
        parsed = _parse_date_candidates(match.group(1))
        if parsed:
            items.append((parsed, path))
    items.sort()
    return items


def _parse_csv_rows(lines: Iterable[str]) -> List[Dict[str, str]]:
    """Parse CSV text into a list of dictionaries with trimmed keys/values."""
    reader = csv.DictReader(lines)
    rows: List[Dict[str, str]] = []
    for row in reader:
        rows.append({k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    if not rows:
        raise RuntimeError("FDA export returned no rows.")
    return rows


def fetch_csv_export() -> Tuple[List[Dict[str, str]], date, Path, bool]:
    """Download (or reuse) the FDA PH drug CSV export.

    Returns:
        rows: Parsed CSV rows.
        catalog_date: Date advertised on the FDA PH site or latest cached date.
        raw_path: File path of the raw CSV backing these rows.
        downloaded: True when a fresh download occurred during this run.
    """

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    existing = _existing_raw_dates(RAW_DIR)
    latest_local_date = existing[-1][0] if existing else None

    with requests.Session() as session:
        landing = session.get(HUMAN_DRUGS_URL, headers=HEADERS, timeout=30)
        landing.raise_for_status()
        as_of = _extract_as_of_date(landing.text)

        target_date = as_of
        downloaded = False

        if latest_local_date and latest_local_date >= as_of:
            # Reuse the most recent cached export when it is at least as new as the site.
            target_date = latest_local_date
            raw_path = RAW_DIR / f"FDA_PH_DRUGS_{target_date.isoformat()}.csv"
            if not raw_path.is_file():
                raise RuntimeError(f"Expected cached raw file {raw_path} is missing.")
            text = raw_path.read_text(encoding="utf-8")
        else:
            raw_path = RAW_DIR / f"FDA_PH_DRUGS_{target_date.isoformat()}.csv"
            if raw_path.is_file():
                text = raw_path.read_text(encoding="utf-8")
            else:
                export = session.get(f"{HUMAN_DRUGS_URL}?export=csv", headers=HEADERS, timeout=120)
                export.raise_for_status()
                text = export.text
                raw_path.write_text(text, encoding="utf-8")
                downloaded = True

    rows = _parse_csv_rows(text.splitlines())
    return rows, target_date, raw_path, downloaded


def normalize_columns(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Rename raw FDA columns into predictable snake_case keys."""
    key_map = {
        "Registration Number": "registration_number",
        "Generic Name": "generic_name",
        "Brand Name": "brand_name",
        "Dosage Strength": "dosage_strength",
        "Dosage Form": "dosage_form",
        "Pharmacologic Category": "pharmacologic_category",
        "Manufacturer": "manufacturer",
        "Country of Origin": "country_of_origin",
        "Application Type": "application_type",
        "Issuance Date": "issuance_date",
        "Expiry Date": "expiry_date",
        "Product Information": "product_information",
    }
    out: List[Dict[str, str]] = []
    for r in rows:
        nr: Dict[str, str] = {}
        for k, v in r.items():
            kk = key_map.get(k, k.lower().replace(" ", "_"))
            # Map or fallback to a deterministic snake_case key.
            nr[kk] = v
        out.append(nr)
    return out


def infer_form_and_route(dosage_form: Optional[str]) -> (Optional[str], Optional[str]):
    """Derive normalized form and route tokens from the FDA dosage form string."""
    if not isinstance(dosage_form, str) or not dosage_form.strip():
        return None, None
    norm = normalize_text(dosage_form)
    form_token = parse_form_from_text(norm)
    route = FORM_TO_ROUTE.get(form_token) if form_token else None
    return form_token, route


# Cache for DrugBank data loaded from file
_DRUGBANK_GENERICS_CACHE: Optional[set] = None
_DRUGBANK_SYNONYMS_CACHE: Optional[Dict[str, str]] = None
_DRUGBANK_AUTOMATON_CACHE = None


def _load_drugbank_data() -> Tuple[set, Dict[str, str]]:
    """Load DrugBank generic names and build synonym mapping.
    
    Returns:
        generics: Set of all known generic names (canonical + lexemes)
        synonyms: Dict mapping each name to its canonical form
    """
    global _DRUGBANK_GENERICS_CACHE, _DRUGBANK_SYNONYMS_CACHE
    if _DRUGBANK_GENERICS_CACHE is not None and _DRUGBANK_SYNONYMS_CACHE is not None:
        return _DRUGBANK_GENERICS_CACHE, _DRUGBANK_SYNONYMS_CACHE
    
    generics: set = set()
    synonyms: Dict[str, str] = {}
    
    # Build synonym groups by drugbank_id
    from collections import defaultdict
    synonym_groups: Dict[str, set] = defaultdict(set)
    canonical_by_id: Dict[str, str] = {}
    
    # Try to load from pipeline inputs
    possible_paths = [
        MODULE_ROOT.parent.parent / "inputs" / "drugs" / "drugbank_generics_master.csv",
        MODULE_ROOT / "output" / "drugbank_generics_master.csv",
    ]
    
    for path in possible_paths:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        db_id = (row.get("drugbank_id") or "").strip()
                        canonical = (row.get("canonical_generic_name") or "").strip().upper()
                        lexeme = (row.get("lexeme") or "").strip().upper()
                        
                        if not db_id:
                            continue
                        
                        # Track canonical name for this drugbank_id
                        if canonical and canonical != "NAN":
                            canonical_by_id[db_id] = canonical
                            synonym_groups[db_id].add(canonical)
                            generics.add(canonical)
                        
                        # Add lexeme as synonym
                        if lexeme and lexeme != "NAN":
                            synonym_groups[db_id].add(lexeme)
                            generics.add(lexeme)
                
                # Build synonym mapping: each name maps to its canonical form
                for db_id, names in synonym_groups.items():
                    canonical = canonical_by_id.get(db_id)
                    if canonical:
                        for name in names:
                            if name != canonical:
                                synonyms[name] = canonical
                
                print(f"[fda_drug] Loaded {len(generics)} DrugBank generics, {len(synonyms)} synonyms from {path}")
                break
            except Exception as e:
                print(f"[fda_drug] Warning: Could not load DrugBank data from {path}: {e}")
    
    _DRUGBANK_GENERICS_CACHE = generics
    _DRUGBANK_SYNONYMS_CACHE = synonyms
    return generics, synonyms


def _load_drugbank_generics() -> set:
    """Load DrugBank generic names from the master file."""
    generics, _ = _load_drugbank_data()
    return generics


def _load_drugbank_synonyms() -> Dict[str, str]:
    """Load DrugBank synonym mapping (name -> canonical form)."""
    _, synonyms = _load_drugbank_data()
    return synonyms


def _build_aho_automaton(generics: set):
    """Build Aho-Corasick automaton for exact generic name matching."""
    global _DRUGBANK_AUTOMATON_CACHE
    if _DRUGBANK_AUTOMATON_CACHE is not None:
        return _DRUGBANK_AUTOMATON_CACHE
    
    try:
        import ahocorasick
        automaton = ahocorasick.Automaton()
        for generic in generics:
            if generic:
                automaton.add_word(generic, generic)
        automaton.make_automaton()
        _DRUGBANK_AUTOMATON_CACHE = automaton
        return automaton
    except ImportError:
        print("[fda_drug] Warning: ahocorasick not available, using set lookup")
        return None


def _is_exact_generic_match(name: str, automaton, generics: set) -> bool:
    """
    Check if name is an EXACT match for a DrugBank generic using Aho-Corasick.
    
    This detects cases like:
    - "Acetylcysteine" (brand) -> exact match to ACETYLCYSTEINE generic -> FLIP
    - "Biogesic" (brand) -> no match -> OK (even though generic is Paracetamol)
    - "Amoxicillin (As Trihydrate)" -> base "AMOXICILLIN" matches -> FLIP
    
    Brand names that CONTAIN a generic as substring are OK (e.g., "Acetylflux" contains "acetyl").
    Only exact matches (full word) indicate a flip.
    """
    if not name:
        return False
    
    upper = name.upper().strip()
    
    # Direct exact match
    if upper in generics:
        return True
    
    # Strip salt forms and check base name
    # e.g., "METFORMIN HYDROCHLORIDE" -> "METFORMIN"
    base = re.sub(r'\s+(HYDROCHLORIDE|HCL|SODIUM|POTASSIUM|CALCIUM|SULFATE|ACETATE|MALEATE|FUMARATE|TARTRATE|CITRATE|PHOSPHATE|CHLORIDE|BESILATE|BESYLATE|MESYLATE|TRIHYDRATE|DIHYDRATE|MONOHYDRATE)\s*$', '', upper, flags=re.IGNORECASE)
    if base != upper and base in generics:
        return True
    
    # Check for "AS" salt forms (e.g., "AMLODIPINE AS BESILATE")
    as_match = re.match(r'^(.+?)\s+AS\s+', upper)
    if as_match:
        base = as_match.group(1).strip()
        if base in generics:
            return True
    
    # Check for "(AS SALT)" patterns (e.g., "AMLODIPINE (AS BESILATE)")
    paren_match = re.match(r'^(.+?)\s*\(AS\s+', upper)
    if paren_match:
        base = paren_match.group(1).strip()
        if base in generics:
            return True
    
    # Check for combination patterns (e.g., "IBUPROFEN + PARACETAMOL")
    # ALL parts must be generics for this to be a flip
    if "+" in upper:
        parts = [p.strip() for p in upper.split("+")]
        all_generic = True
        for part in parts:
            part_base = re.sub(r'\s+(HYDROCHLORIDE|HCL|SODIUM|POTASSIUM|CALCIUM|SULFATE|ACETATE|MALEATE|FUMARATE|TARTRATE|CITRATE|PHOSPHATE|CHLORIDE|BESILATE|BESYLATE|MESYLATE)\s*$', '', part, flags=re.IGNORECASE)
            if part_base not in generics:
                all_generic = False
                break
        if all_generic:
            return True
    
    return False


def _detect_brand_generic_flip(brand: str, generic: str, generics: Optional[set] = None, automaton=None) -> bool:
    """
    Detect if brand and generic columns are likely swapped.
    
    Uses EXACT matching - brand names that contain a generic as substring are OK.
    Only flags when brand IS a generic name (not just contains one).
    
    Examples:
    - brand="Acetylcysteine", generic="Nacel" -> FLIP (brand is exact generic)
    - brand="Acetylflux", generic="Acetylcysteine" -> OK (brand contains "acetyl" but isn't a generic)
    - brand="Biogesic", generic="Paracetamol" -> OK (brand is not a generic)
    """
    if not brand or not generic:
        return False
    
    # Load DrugBank generics if not provided
    if generics is None:
        generics = _load_drugbank_generics()
    
    # Build automaton if not provided
    if automaton is None:
        automaton = _build_aho_automaton(generics)
    
    brand_is_generic = _is_exact_generic_match(brand, automaton, generics)
    generic_is_generic = _is_exact_generic_match(generic, automaton, generics)
    
    # Clear flip: brand column has an exact generic match, generic column doesn't
    if brand_is_generic and not generic_is_generic:
        return True
    
    return False


def build_brand_map(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Trim, dedupe, and enrich FDA rows for downstream brand→generic lookups.
    
    Also detects and fixes brand/generic column flips by checking against DrugBank generics.
    Uses EXACT matching - brand names containing generics as substrings are OK.
    """
    seen = set()
    out: List[Dict[str, str]] = []
    flip_count = 0
    
    # Load DrugBank generics and build automaton once for all rows
    drugbank_generics = _load_drugbank_generics()
    automaton = _build_aho_automaton(drugbank_generics)
    
    for r in rows:
        brand = (r.get("brand_name") or "").strip()
        generic = (r.get("generic_name") or "").strip()
        if not brand or not generic:
            continue

        # Detect and fix brand/generic flip using exact matching
        if _detect_brand_generic_flip(brand, generic, drugbank_generics, automaton):
            brand, generic = generic, brand
            flip_count += 1

        dosage_form = (r.get("dosage_form") or "").strip()
        dosage_strength = (r.get("dosage_strength") or "").strip()
        regno = (r.get("registration_number") or "").strip()

        form_token, route = infer_form_and_route(dosage_form)

        key = (
            brand.lower(),
            generic.lower(),
            (form_token or "").lower(),
            (route or "").lower(),
            dosage_strength.lower(),
        )
        if key in seen:
            continue
        seen.add(key)

        out.append(
            {
                "brand_name": brand,
                "generic_name": generic,
                "dosage_form": form_token or dosage_form or "",
                "route": route or "",
                "dosage_strength": dosage_strength,
                "registration_number": regno,
            }
        )
    
    if flip_count > 0:
        print(f"[fda_drug] Fixed {flip_count} brand/generic flips")
    
    return out


def main() -> None:
    """CLI wrapper that downloads, normalizes, and writes the brand-map export."""
    ap = argparse.ArgumentParser(description="Build FDA PH brand→generic map (CSV + Parquet export)")
    ap.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    ap.add_argument("--outfile", default=None, help="Optional explicit output CSV filename")
    args = ap.parse_args()

    rows, catalog_date, raw_path, downloaded = fetch_csv_export()
    rows = normalize_columns(rows)
    brand_map = build_brand_map(rows)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    date_tag = catalog_date.isoformat()
    out_csv = Path(args.outfile) if args.outfile else outdir / f"fda_drug_{date_tag}.csv"

    fieldnames = ["brand_name", "generic_name", "dosage_form", "route", "dosage_strength", "registration_number"]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(brand_map)
    table = pa.Table.from_pylist(brand_map, schema=pa.schema([(name, pa.string()) for name in fieldnames]))
    pq.write_table(table, out_csv.with_suffix(".parquet"))

    inputs_dir = MODULE_ROOT.parent.parent / "inputs" / "drugs"
    try:
        inputs_dir.mkdir(parents=True, exist_ok=True)
        for path in [out_csv, out_csv.with_suffix(".parquet")]:
            if path.is_file():
                shutil.copy2(path, inputs_dir / path.name)
    except Exception:
        pass

    status_note = "downloaded" if downloaded else "reused cached"
    print(f"FDA PH drug catalog ({catalog_date.isoformat()}) {status_note}: raw={raw_path}")


if __name__ == "__main__":
    main()
