#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import requests

MODULE_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = MODULE_ROOT / "output"
RAW_DIR = MODULE_ROOT / "raw"

BASE_URL = "https://verification.fda.gov.ph"
FOOD_PRODUCTS_URL = f"{BASE_URL}/All_FoodProductslist.php"
FOOD_EXPORT_URL = f"{FOOD_PRODUCTS_URL}?export=csv"

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "close",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/109.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-US,en;q=0.9,fil;q=0.4",
    "en-SG,en;q=0.9",
]

PAGE_SIZES = ("100",)
DEFAULT_TIMEOUT = None

RECORD_SUMMARY_RX = re.compile(r"Records\s+\d+\s+to\s+([\d,]+)\s+of\s+([\d,]+)", re.IGNORECASE)

# Pattern to match dated files: name_YYYY-MM-DD.ext
DATED_FILE_PATTERN = re.compile(r"^(.+?)_(\d{4}-\d{2}-\d{2})(?:_.*)?(\.\w+)$")


def _purge_old_dated_files(directory: Path, quiet: bool = True) -> int:
    """Remove all but the latest version of dated files."""
    if not directory.exists():
        return 0
    
    groups: Dict[Tuple[str, str], List[Tuple[str, Path]]] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = DATED_FILE_PATTERN.match(path.name)
        if match:
            base_name, date_str, ext = match.groups()
            key = (base_name, ext)
            if key not in groups:
                groups[key] = []
            groups[key].append((date_str, path))
    
    deleted = 0
    for (base_name, ext), files in groups.items():
        if len(files) <= 1:
            continue
        files.sort(key=lambda x: x[0], reverse=True)
        latest_date, latest_path = files[0]
        for date_str, path in files[1:]:
            try:
                path.unlink()
                deleted += 1
                if not quiet:
                    print(f"[purge] Removed: {path.name}")
            except Exception:
                pass
    return deleted


def _render_progress(
    prefix: str,
    current: int,
    total: Optional[int],
    *,
    done: bool = False,
    verbose: bool = True,
) -> None:
    if not verbose:
        return
    if total and total > 0:
        msg = f"{prefix}: {current:,}/{total:,}"
    else:
        msg = f"{prefix}: {current:,}"
    sys.stdout.write("\r" + msg.ljust(80))
    sys.stdout.flush()
    if done:
        sys.stdout.write("\n")
        sys.stdout.flush()


class _FoodTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_tbody = False
        self.in_row = False
        self.in_cell = False
        self.current_row: List[str] = []
        self.current_text: List[str] = []
        self.rows: List[List[str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "table" and attrs_dict.get("id") == "tbl_All_FoodProductslist":
            self.in_table = True
            return
        if not self.in_table:
            return
        if tag == "tbody":
            self.in_tbody = True
            return
        if self.in_tbody and tag == "tr":
            self.in_row = True
            self.current_row = []
            return
        if self.in_row and tag == "td":
            self.in_cell = True
            self.current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.in_table:
            self.in_table = False
            self.in_tbody = False
            self.in_row = False
            self.in_cell = False
            return
        if not self.in_table:
            return
        if tag == "tbody":
            self.in_tbody = False
            return
        if tag == "tr" and self.in_row:
            self.in_row = False
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
            return
        if tag == "td" and self.in_cell:
            text = "".join(self.current_text).strip()
            self.current_row.append(text)
            self.in_cell = False
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_text.append(data)


def _row_key(row: Dict[str, str]) -> Tuple[str, str, str, str]:
    return (
        (row.get("brand_name") or "").strip().lower(),
        (row.get("product_name") or "").strip().lower(),
        (row.get("company_name") or "").strip().lower(),
        (row.get("registration_number") or "").strip().lower(),
    )


def _cells_to_rows(cells_list: Iterable[List[str]]) -> List[Dict[str, str]]:
    parsed: List[Dict[str, str]] = []
    for cells in cells_list:
        if len(cells) < 5:
            continue
        parsed.append(
            {
                "registration_number": cells[1].strip(),
                "company_name": cells[2].strip(),
                "product_name": cells[3].strip(),
                "brand_name": cells[4].strip(),
            }
        )
    return parsed


def _parse_record_summary(html: str) -> Optional[int]:
    match = RECORD_SUMMARY_RX.search(html)
    if not match:
        return None
    total = match.group(2).replace(",", "")
    try:
        return int(total)
    except ValueError:
        return None


def _parse_food_rows(html: str) -> Tuple[List[Dict[str, str]], Optional[int]]:
    parser = _FoodTableParser()
    parser.feed(html)
    return _cells_to_rows(parser.rows), _parse_record_summary(html)

def _fetch_total_entries(timeout: Optional[int]) -> Optional[int]:
    """Fetch a single page to read the total entry count."""
    try:
        resp = requests.get(
            FOOD_PRODUCTS_URL,
            params={"recperpage": PAGE_SIZES[0]},
            headers=_random_headers(),
            timeout=timeout or 30,
        )
        resp.raise_for_status()
        _, total = _parse_food_rows(resp.text)
        return total
    except Exception:
        return None


def _dedupe_rows(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set[Tuple[str, str, str, str]] = set()
    unique: List[Dict[str, str]] = []
    for row in rows:
        key = _row_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _random_headers() -> Dict[str, str]:
    headers = BASE_HEADERS.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
    headers["Accept-Language"] = random.choice(ACCEPT_LANGUAGES)
    headers["Referer"] = FOOD_PRODUCTS_URL
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    return headers


def _load_existing_catalog(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for entry in reader:
            rows.append(
                {
                    "brand_name": (entry.get("brand_name") or "").strip(),
                    "product_name": (entry.get("product_name") or "").strip(),
                    "company_name": (entry.get("company_name") or "").strip(),
                    "registration_number": (entry.get("registration_number") or "").strip(),
                }
            )
    return _dedupe_rows(rows)


def _fetch_page(
    session: requests.Session,
    *,
    recperpage: str,
    start: Optional[int],
    timeout: Optional[int],
    retries: Optional[int] = None,
    backoff: float = 5.0,
    verbose: bool = True,
) -> str:
    params: Dict[str, str] = {"recperpage": recperpage}
    if start is not None:
        params["start"] = str(start)
    attempt = 0
    while True:
        attempt += 1
        try:
            t0 = time.perf_counter()
            if verbose:
                print(
                    f"→ Fetch recperpage={recperpage} start={start or 1} (attempt {attempt})",
                    flush=True,
                )
            timeout_value = timeout if timeout is not None else None
            delay = random.uniform(1.5, 4.0)
            if verbose:
                print(f"   waiting {delay:.2f}s before request", flush=True)
            time.sleep(delay)
            response = session.get(
                FOOD_PRODUCTS_URL,
                params=params,
                headers=_random_headers(),
                timeout=timeout_value,
            )
            response.raise_for_status()
            if verbose:
                elapsed = time.perf_counter() - t0
                print(
                    f"← Done  recperpage={recperpage} start={start or 1} in {elapsed:.2f}s",
                    flush=True,
                )
            return response.text
        except Exception as exc:  # noqa: BLE001
            if retries is not None and attempt >= retries:
                raise
            wait = backoff * attempt
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except ValueError:
                            wait = max(wait, backoff * (attempt + 1))
                    else:
                        wait = max(wait, backoff * (attempt + 1))
                elif status >= 500:
                    wait = max(wait, backoff * (attempt + 2))
                elif status == 403:
                    wait = max(wait, backoff * (attempt + 2))
            if verbose:
                print(
                    f"! Request failed (recperpage={recperpage}, start={start}): {exc}. Retrying in {wait:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            session.cookies.clear()
            time.sleep(wait)


def _extract_next_start_values(html: str, *, current: int, recperpage: str) -> List[int]:
    values: set[int] = set()
    for href in re.findall(r"href=\"([^\"]+)\"", html, flags=re.IGNORECASE):
        if "All_FoodProductslist.php" not in href:
            continue
        parsed = urlparse(urljoin(FOOD_PRODUCTS_URL, href))
        query = parse_qs(parsed.query)
        if query.get("recperpage", [""])[0].lower() != recperpage.lower():
            continue
        for start_val in query.get("start", []):
            try:
                num = int(start_val)
            except ValueError:
                continue
            if num > current:
                values.add(num)
    return sorted(values)


def _scrape_paginated(
    session: requests.Session,
    recperpage: str,
    timeout: Optional[int],
    expected_total: Optional[int],
    *,
    seen: set[Tuple[str, str, str, str]],
    aggregated: List[Dict[str, str]],
    flush: Optional[Callable[[List[Dict[str, str]]], None]],
    verbose: bool,
) -> Tuple[Optional[int], List[str]]:
    html_pages: List[str] = []
    start = 1
    previous_first_reg: Optional[str] = None
    last_report = time.monotonic()

    while True:
        html = _fetch_page(
            session,
            recperpage=recperpage,
            start=start,
            timeout=timeout,
            verbose=verbose,
        )
        html_pages.append(html)
        rows, total = _parse_food_rows(html)
        if expected_total is None and total is not None:
            expected_total = total
        if not rows:
            break

        first_reg = rows[0].get("registration_number")
        if previous_first_reg is not None and first_reg == previous_first_reg:
            break
        previous_first_reg = first_reg

        new_rows = 0
        for row in rows:
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            aggregated.append(row)
            new_rows += 1

        if new_rows and flush:
            flush(aggregated)

        now = time.monotonic()
        if now - last_report >= 1:
            _render_progress(
                f"Scraping recperpage={recperpage}",
                len(aggregated),
                expected_total,
                verbose=verbose,
            )
            last_report = now

        if expected_total is not None and len(aggregated) >= expected_total:
            break

        if len(rows) < int(recperpage):
            break

        next_candidates = _extract_next_start_values(html, current=start, recperpage=recperpage)
        if next_candidates:
            start = min(next_candidates)
        else:
            start += len(rows)

    if aggregated:
        _render_progress(
            f"Scraping recperpage={recperpage}",
            len(aggregated),
            expected_total,
            done=True,
            verbose=verbose,
        )

    return expected_total, html_pages


def scrape_food_catalog(
    timeout: Optional[int] = DEFAULT_TIMEOUT,
    *,
    verbose: bool = True,
    existing_rows: Optional[List[Dict[str, str]]] = None,
    flush: Optional[Callable[[List[Dict[str, str]]], None]] = None,
) -> Tuple[List[Dict[str, str]], List[str], str]:
    with requests.Session() as session:
        aggregated: List[Dict[str, str]] = list(existing_rows or [])
        seen: set[Tuple[str, str, str, str]] = {_row_key(row) for row in aggregated}
        if verbose and aggregated:
            print(f"Loaded {len(aggregated):,} existing rows; continuing scrape.", flush=True)

        expected_total: Optional[int] = None
        html_pages_all: List[str] = []

        for size in PAGE_SIZES:
            if verbose:
                print(f"=== Starting scrape with recperpage={size}", flush=True)
            expected_total, html_pages = _scrape_paginated(
                session,
                size,
                timeout,
                expected_total,
                seen=seen,
                aggregated=aggregated,
                flush=flush,
                verbose=verbose,
            )
            html_pages_all.extend(html_pages)

            if expected_total is not None and len(aggregated) >= expected_total:
                if verbose:
                    print(
                        f"=== Completed scrape with recperpage={size} (rows={len(aggregated):,}/{expected_total:,})",
                        flush=True,
                    )
                break

        if not aggregated:
            raise RuntimeError("Unable to scrape FDA PH food products list (no rows captured).")

        return list(aggregated), html_pages_all, size


def _normalize_column_name(name: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return clean


def _load_export_file(path: Path) -> List[Dict[str, str]]:
    """Load the downloaded export (CSV text) and coerce to the canonical column set."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to read export file {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    raw_rows = list(reader)
    if not raw_rows:
        raise RuntimeError(f"Failed to read export file {path}: empty payload")

    rows: List[Dict[str, str]] = []
    for raw in raw_rows:
        mapped: Dict[str, str] = {
            "brand_name": "",
            "product_name": "",
            "company_name": "",
            "registration_number": "",
        }
        for key, value in raw.items():
            norm = _normalize_column_name(str(key))
            val = (value or "").strip() if isinstance(value, str) else "" if value is None else str(value).strip()
            if "registration" in norm and "number" in norm:
                mapped["registration_number"] = val
            elif "company" in norm:
                mapped["company_name"] = val
            elif "product" in norm:
                mapped["product_name"] = val
            elif "brand" in norm:
                mapped["brand_name"] = val
        rows.append(mapped)
    return _dedupe_rows(rows)


def _download_export_if_needed(
    *,
    total_remote: Optional[int],
    existing_count: int,
    outdir: Path,
    timeout: Optional[int],
    quiet: bool,

) -> Tuple[Optional[List[Dict[str, str]]], Optional[Path], Optional[str]]:
    """Attempt the official CSV/Excel export and return rows, path, and an error message if any."""

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    export_path = outdir / f"fda_food_export_{date_tag}.csv"

    session = requests.Session()
    try:
        session.get(
            FOOD_PRODUCTS_URL,
            params={"start": 1},
            headers=_random_headers(),
            timeout=timeout or 60,
        )
    except Exception:
        pass

    try:
        resp = session.get(
            FOOD_EXPORT_URL,
            headers={
                **_random_headers(),
                "Accept": "text/csv,application/vnd.ms-excel,application/octet-stream;q=0.8,text/html;q=0.5",
                "Referer": FOOD_PRODUCTS_URL,
            },
            timeout=timeout or 300,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            return None, None, f"export responded with HTML content-type: {content_type}"
        export_path.write_bytes(resp.content)
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[download] Failed to fetch Excel export: {exc}", flush=True)
        export_path.unlink(missing_ok=True)
        return None, None, str(exc)

    try:
        probe = export_path.read_bytes()[:512]
        if probe and b"<html" in probe.lower():
            return None, export_path, "server returned HTML instead of CSV (likely download gating)"
        rows = _load_export_file(export_path)
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[download] Unable to parse Excel export: {exc}", flush=True)
        return None, export_path, str(exc)

    if total_remote is not None and len(rows) < total_remote:
        gap = total_remote - len(rows)
        if gap > 5:
            if not quiet:
                print(
                    f"[download] Parsed {len(rows):,}/{total_remote:,} rows from export; not all rows present.",
                    flush=True,
                )
            return None, export_path, "export missing expected rows"
        if not quiet:
            print(
                f"[download] Parsed {len(rows):,}/{total_remote:,} rows from export; accepting with {gap} missing.",
                flush=True,
            )

    if not quiet:
        print(f"[download] Using Excel export with {len(rows):,} rows.", flush=True)
    return rows, export_path, None


def build_catalog(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    filtered: List[Dict[str, str]] = []
    for row in rows:
        brand = (row.get("brand_name") or "").strip()
        product = (row.get("product_name") or "").strip()
        company = (row.get("company_name") or "").strip()
        reg = (row.get("registration_number") or "").strip()
        if not (brand or product or company or reg):
            continue
        filtered.append(
            {
                "brand_name": brand,
                "product_name": product,
                "company_name": company,
                "registration_number": reg,
            }
        )
    return _dedupe_rows(filtered)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape the FDA Philippines food product catalog via paginated 100-row views."
    )
    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for the processed CSV")
    parser.add_argument("--outfile", default=None, help="Processed output filename (defaults to timestamped)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Optional per-request timeout (seconds); export download uses up to 5 minutes.",
    )
    parser.add_argument(
        "--allow-scrape",
        action="store_true",
        default=False,
        help="Enable HTML scraping fallback when export download is unavailable or incomplete.",
    )
    parser.add_argument("--force", action="store_true", help="Force re-scraping even if output exists")
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose progress output")
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    run_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    outfile = args.outfile or f"fda_food_{run_tag}.csv"
    out_csv = outdir / outfile

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    
    # Auto-purge old dated files
    _purge_old_dated_files(outdir, quiet=args.quiet)

    # Find existing catalog - check today's file first, then any previous file
    existing_rows: List[Dict[str, str]] = []
    existing_catalog_path: Optional[Path] = None
    
    if out_csv.exists() and not args.force:
        existing_catalog_path = out_csv
    elif not args.force:
        # Look for any existing fda_food_*.csv (not export files)
        existing_files = sorted(
            [p for p in outdir.glob("fda_food_*.csv") if "export" not in p.name],
            reverse=True  # Most recent first
        )
        if existing_files:
            existing_catalog_path = existing_files[0]
    
    if existing_catalog_path:
        existing_rows = _load_existing_catalog(existing_catalog_path)
        if not args.quiet:
            print(f"Found {len(existing_rows):,} rows in {existing_catalog_path.name}.", flush=True)
    elif args.force and out_csv.exists():
        if not args.quiet:
            print(f"Discarding existing catalog at {out_csv} (--force).", flush=True)
        out_csv.unlink()

    fieldnames = ["brand_name", "product_name", "company_name", "registration_number"]

    def _flush(rows: List[Dict[str, str]]) -> None:
        catalog_snapshot = build_catalog(rows)
        tmp_path = out_csv.with_suffix(out_csv.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(catalog_snapshot)
        tmp_path.replace(out_csv)

    # Initialize variables that may be used in final output
    download_rows: Optional[List[Dict[str, str]]] = None
    export_path: Optional[Path] = None
    html_pages: List[str] = []
    page_size = PAGE_SIZES[0]
    
    # If we have existing rows and not forcing, use cached data (skip download/scrape)
    if existing_rows and not args.force:
        if not args.quiet:
            print(f"[cache] Using {len(existing_rows):,} cached rows (use --force to refresh).", flush=True)
        catalog_rows = existing_rows
    else:
        # Try official export when the site advertises more rows than we have locally.
        total_remote = _fetch_total_entries(args.timeout)
        download_needed = (not existing_rows) or (total_remote is not None and len(existing_rows) < total_remote)
        download_error: Optional[str] = None
        if download_needed:
            download_rows, export_path, download_error = _download_export_if_needed(
                total_remote=total_remote,
                existing_count=len(existing_rows),
                outdir=outdir,
                timeout=60,  # Reduced from 300s - fail fast
                quiet=args.quiet,
            )

        if download_rows is not None:
            catalog_rows = download_rows
            html_pages = []
            page_size = PAGE_SIZES[0]
        elif download_needed:
            if not args.allow_scrape:
                reason = download_error or "download failed"
                # If we have ANY existing rows, use them instead of failing
                if existing_rows:
                    if not args.quiet:
                        print(f"[fallback] Download failed ({reason}); using {len(existing_rows):,} cached rows.", flush=True)
                    catalog_rows = existing_rows
                    html_pages = []
                    page_size = PAGE_SIZES[0]
                else:
                    raise RuntimeError(
                        f"FDA food export download unavailable ({reason}); no cached data available. Use --allow-scrape for fallback."
                    )
            else:
                rows, html_pages, page_size = scrape_food_catalog(
                    timeout=args.timeout,
                    verbose=not args.quiet,
                    existing_rows=existing_rows,
                    flush=_flush,
                )
                catalog_rows = rows
        else:
            catalog_rows = existing_rows
            html_pages = []
            page_size = PAGE_SIZES[0]

    catalog = build_catalog(catalog_rows)
    _flush(catalog)

    fieldnames = ["brand_name", "product_name", "company_name", "registration_number"]
    out_parquet = out_csv.with_suffix(".parquet")
    table = pa.Table.from_pylist(catalog, schema=pa.schema([(name, pa.string()) for name in fieldnames]))
    pq.write_table(table, out_parquet)

    inputs_dir = MODULE_ROOT.parent.parent / "inputs" / "drugs"
    try:
        inputs_dir.mkdir(parents=True, exist_ok=True)
        for path in [out_csv, out_parquet]:
            if path.is_file():
                shutil.copy2(path, inputs_dir / path.name)
    except Exception:
        pass

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    raw_path = RAW_DIR / f"FDA_PH_FOOD_PRODUCTS_{page_size}_{timestamp}.html"
    raw_content = "\n<!-- page break -->\n".join(html_pages) if html_pages else ""
    raw_path.write_text(raw_content, encoding="utf-8")

    action = "downloaded export" if download_rows is not None else "scraped HTML"
    print(
        "FDA PH food catalog {action} via recperpage={page} (raw={raw}, processed={processed}, entries={count})".format(
            action=action,
            page=page_size,
            raw=export_path if download_rows is not None else raw_path,
            processed=out_csv,
            count=len(catalog),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
