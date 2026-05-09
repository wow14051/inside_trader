#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SEC_BASE = "https://www.sec.gov/Archives/"
TICKER_URL = "https://www.sec.gov/include/ticker.txt"
DEFAULT_SEC_USER_AGENT = "SEC4-Insider-Bot AdminContact@example.com"
DEFAULT_SEC_CONTACT_EMAIL = "contact@example.com"
DEFAULT_MINIMUM_USD = 200_000
DEFAULT_MAX_LOOKBACK_DAYS = 7
DEFAULT_DEBUG = True
HTTP_TIMEOUT = 30
DEFAULT_INDEX_WORKERS = 4
DEFAULT_FORM4_WORKERS = 8
DINGTALK_MAX_BODY_BYTES = 20_000
DINGTALK_SAFE_BODY_BYTES = DINGTALK_MAX_BODY_BYTES - 2_000
DESKTOP_TICKER_EXTENSIONS = {".ebk", ".txt", ".csv"}
MAX_DESKTOP_TICKER_FILE_BYTES = 2_000_000

TICKER_TOKEN_RE = re.compile(r"^[A-Z]{1,6}(?:[-.][A-Z0-9]{1,2})?$")
TICKER_LABELS = {
    "ticker",
    "tickers",
    "symbol",
    "symbols",
    "stock",
    "stockcode",
    "stocks",
    "stocksymbol",
    "code",
    "codes",
    "securitycode",
    "股票",
    "股票代码",
    "证券代码",
    "代码",
}
TICKER_STOPWORDS = {
    "ACTIONS",
    "AMEX",
    "BUY",
    "CIK",
    "CODE",
    "CSV",
    "DATE",
    "FALSE",
    "FIXME",
    "LIST",
    "MARKET",
    "NAME",
    "NASDAQ",
    "NULL",
    "NYSE",
    "PRICE",
    "SEC",
    "SELL",
    "SHARE",
    "SHARES",
    "STOCK",
    "STOCKS",
    "SYMBOL",
    "SYMBOLS",
    "TICKER",
    "TICKERS",
    "TODO",
    "TRUE",
    "USD",
    "VOLUME",
    "WATCH",
    "WATCHLIST",
}
STOCK_LIST_FILENAME_HINTS = (
    "ticker",
    "tickers",
    "symbol",
    "symbols",
    "stock",
    "stocks",
    "watch",
    "watchlist",
    "portfolio",
    "自选",
    "股票",
    "证券",
    "持仓",
)

FALLBACK_TICKER_MAP = {
    "BRKB": "1067983",
    "BRK-B": "1067983",
    "MSFT": "0000789019",
    "ZTS": "0001555285",
    "STZ": "0001593873",
}

debug_enabled = DEFAULT_DEBUG


@dataclass
class AlertEntry:
    owner_name: str
    position: str
    kind: str
    shares: int
    price: float
    amount: float
    is_10b5_1: bool
    transaction_date: str
    shares_owned_after: int


@dataclass
class MasterIndex:
    index_date: str
    content: str


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv = expand_short_cli_args(argv)
    if should_show_help(argv):
        print(usage_text())
        return 0
    try:
        options = parse_options(argv)
        stock_list_name = first_non_blank(options.get("stock-list"), options.get("stocklist"))
        tickers_arg = first_non_blank(options.get("tickers"))
        if not tickers_arg and stock_list_name:
            named_tickers, source_label = discover_tickers_from_named_stock_list(stock_list_name)
            if named_tickers:
                tickers_arg = ",".join(named_tickers)
                print(f"Loaded {len(named_tickers)} ticker(s) from stock list: {source_label}")
            else:
                print(
                    f"Stock list '{stock_list_name}' was not found or did not look like a stock list. "
                    "Falling back to default ticker resolution."
                )

        if not tickers_arg:
            tickers_arg = first_non_blank(os.getenv("TICKERS"), options.get("positional"))
        minimum_usd = parse_int(
            first_non_blank(options.get("threshold"), os.getenv("THRESHOLD_USD")),
            DEFAULT_MINIMUM_USD,
        )
        max_lookback_days = parse_int(
            first_non_blank(options.get("lookback"), os.getenv("LOOKBACK_DAYS")),
            DEFAULT_MAX_LOOKBACK_DAYS,
        )
        set_debug(
            parse_bool(
                first_non_blank(options.get("debug"), os.getenv("DEBUG")),
                DEFAULT_DEBUG,
            )
        )

        if not tickers_arg:
            discovered_tickers, source_label = discover_tickers_from_stock_list_files()
            if discovered_tickers:
                tickers_arg = ",".join(discovered_tickers)
                print(f"Loaded {len(discovered_tickers)} ticker(s) from {source_label} stock list file(s).")
            else:
                print(
                    "No tickers provided. Use --tickers=..., TICKERS env, "
                    "or a .ebk/.txt/.csv stock list in the project directory or on Desktop."
                )
                return 0

        tickers = parse_tickers(tickers_arg)
        if not tickers:
            print("No valid tickers found in input.")
            return 0

        log_debug(f"Debug mode enabled: {debug_enabled}")
        log_debug(f"Tickers: {','.join(tickers)}")
        log_debug(f"Threshold: {minimum_usd}")
        log_debug(f"Lookback days: {max_lookback_days}")

        ticker_to_cik = download_ticker_mapping()
        if not ticker_to_cik:
            print("Failed to download SEC ticker mapping.", file=sys.stderr)
            return 0

        cik_to_requested_ticker: dict[str, str] = {}
        ciks: set[str] = set()
        for ticker in tickers:
            cik = find_cik_for_ticker(ticker, ticker_to_cik)
            if cik:
                normalized = normalize_cik(cik)
                ciks.add(normalized)
                cik_to_requested_ticker[normalized] = ticker
                log_debug(f"Ticker mapped: {ticker} -> {normalized}")
            else:
                print(f"Warning: ticker not found in SEC mapping: {ticker}", file=sys.stderr)

        if not ciks:
            print("No valid CIKs found for provided tickers.", file=sys.stderr)
            return 0

        current_date = datetime.now(ZoneInfo("America/New_York")).date()
        form4_urls: list[str] = []
        master_index = find_master_index(current_date, max_lookback_days)
        if master_index:
            form4_urls.extend(parse_master_idx(master_index.content, ciks))
            log_debug(f"Master index lookup returned {len(form4_urls)} Form 4 URLs.")
        else:
            log_debug(
                f"Unable to find a valid SEC master index in the last {max_lookback_days} days."
            )

        if not form4_urls:
            log_debug("No Form 4 URLs in master index. Falling back to SEC browse API...")
            form4_urls.extend(fetch_form4_urls_from_edgar_browse(ciks, max_lookback_days))
            log_debug(f"Browse API fallback returned {len(form4_urls)} Form 4 XML URLs.")

        if not form4_urls:
            msg = (
                f"No Form 4 filings found for {', '.join(tickers)} in the last "
                f"{max_lookback_days} days."
            )
            print(msg)
            send_notification(build_missing_notification(tickers, "No Form 4 filings found"))
            return 0

        all_alerts, processed_count, failed_count = process_form4_urls(
            form4_urls, minimum_usd, cik_to_requested_ticker
        )

        if processed_count == 0 and failed_count > 0:
            raise RuntimeError(f"Failed to process any of the {failed_count} Form 4 filings found.")

        filtered_alerts = {
            ticker: all_alerts[ticker]
            for ticker in tickers
            if ticker in all_alerts and all_alerts[ticker]
        }

        if not filtered_alerts:
            no_trade_msg = "📭 No insider transactions found today."
            print(no_trade_msg)
            send_notification(no_trade_msg)
            return 0

        index_date = master_index.index_date if master_index else date.today().isoformat()
        message = build_grouped_notification(filtered_alerts, index_date)
        notified = send_notification(message)
        alert_count = sum(len(alerts) for alerts in filtered_alerts.values())
        print(
            f"Found {alert_count} alert(s) in {len(filtered_alerts)} ticker(s). "
            f"Notification sent: {str(notified).lower()}"
        )
        if not notified:
            print(message)
        return 0
    except Exception as exc:
        error_msg = str(exc) or "Unknown error"
        print(f"Fatal error: {error_msg}", file=sys.stderr)
        traceback.print_exc()
        if not any(
            text in error_msg
            for text in (
                "No Form 4 filings found",
                "No large insider transactions found",
                "No valid CIKs found",
            )
        ):
            send_error_notification(f"Insider Bot Error: {error_msg}")
        return 1


def expand_short_cli_args(args: list[str]) -> list[str]:
    if not args or args[0].startswith("-") or not is_positive_int_arg(args[0]):
        return args

    expanded = [f"--lookback={args[0]}"]
    if len(args) > 1 and not args[1].startswith("-"):
        expanded.append(f"--stock-list={args[1]}")
        expanded.extend(args[2:])
    else:
        expanded.extend(args[1:])
    return expanded


def is_positive_int_arg(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]*", value.strip()))


def should_show_help(args: Iterable[str]) -> bool:
    return any(arg in {"-h", "--help", "help"} for arg in args)


def usage_text() -> str:
    return """Inside Trader Insider Bot

Usage:
  sib
  sib 7
  sib 7 stocklist
  python stock_insider_bot.py --tickers=AAPL,MSFT --lookback=7
  python stock_insider_bot.py --stock-list=stocklist --lookback=7

sib arguments:
  first argument   lookback days
  second argument  stock list file name, extension optional

Stock-list lookup searches the project directory first, then Desktop.
If the named list is not found, the bot falls back to the normal ticker logic."""


def parse_options(args: Iterable[str]) -> dict[str, str | None]:
    options: dict[str, str | None] = {}
    positional = None
    for arg in args:
        if not arg:
            continue
        if arg.startswith("--"):
            normalized = arg[2:]
            if "=" in normalized:
                key, value = normalized.split("=", 1)
                options[key.lower()] = value
            else:
                options[normalized.lower()] = "true"
        elif positional is None:
            positional = arg
    options["positional"] = positional
    return options


def first_non_blank(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value
    return None


def set_debug(enabled: bool) -> None:
    global debug_enabled
    debug_enabled = enabled


def log_debug(message: str) -> None:
    if debug_enabled:
        print(f"DEBUG: {message}")


def parse_int(value: str | None, fallback: int) -> int:
    try:
        return int(value.strip()) if value and value.strip() else fallback
    except ValueError:
        return fallback


def env_int(name: str, fallback: int) -> int:
    return max(1, parse_int(os.getenv(name), fallback))


def parse_bool(value: str | None, fallback: bool) -> bool:
    if not value or not value.strip():
        return fallback
    return value.strip().lower() not in {"false", "0", "no", "off"}


def parse_tickers(tickers_arg: str) -> list[str]:
    return sorted(unique_tickers(ticker.strip().upper() for ticker in tickers_arg.split(",") if ticker.strip()))


def discover_tickers_from_stock_list_files() -> tuple[list[str], str]:
    project_tickers = discover_tickers_from_directories(project_stock_list_directories(), "project")
    if project_tickers:
        return project_tickers, "project"
    return discover_tickers_from_desktop(), "Desktop"


def discover_tickers_from_named_stock_list(name: str) -> tuple[list[str], str | None]:
    path = find_stock_list_file_by_name(name, project_stock_list_directories())
    if path is None:
        path = find_stock_list_file_by_name(name, desktop_directories())
    if path is None:
        return [], None

    tickers = parse_stock_list_file(path)
    if not tickers:
        return [], str(path)
    return unique_tickers(tickers), str(path)


def find_stock_list_file_by_name(name: str, directories: Iterable[Path]) -> Path | None:
    query = clean_stock_list_query(name)
    if not query:
        return None

    direct_path = Path(os.path.expandvars(os.path.expanduser(query)))
    if direct_path.is_file() and direct_path.suffix.lower() in DESKTOP_TICKER_EXTENSIONS:
        return direct_path

    query_path = Path(query)
    query_name = query_path.name.lower()
    query_stem = query_path.stem.lower()
    query_has_supported_suffix = query_path.suffix.lower() in DESKTOP_TICKER_EXTENSIONS
    exact_matches: list[Path] = []
    fuzzy_matches: list[Path] = []

    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: stock_list_match_sort_key(item)):
            if not path.is_file() or path.suffix.lower() not in DESKTOP_TICKER_EXTENSIONS:
                continue
            path_name = path.name.lower()
            path_stem = path.stem.lower()
            if query_has_supported_suffix:
                if path_name == query_name:
                    exact_matches.append(path)
                elif query_name in path_name:
                    fuzzy_matches.append(path)
            else:
                if path_stem == query_stem:
                    exact_matches.append(path)
                elif query_stem in path_stem:
                    fuzzy_matches.append(path)

    return first_path(exact_matches) or first_path(fuzzy_matches)


def clean_stock_list_query(name: str) -> str:
    return name.strip().strip("\"'")


def stock_list_match_sort_key(path: Path) -> tuple[int, str]:
    extension_order = {".ebk": 0, ".csv": 1, ".txt": 2}
    return extension_order.get(path.suffix.lower(), 99), path.name.lower()


def first_path(paths: list[Path]) -> Path | None:
    return paths[0] if paths else None


def discover_tickers_from_desktop() -> list[str]:
    return discover_tickers_from_directories(desktop_directories(), "Desktop")


def discover_tickers_from_directories(directories: Iterable[Path], label: str) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in DESKTOP_TICKER_EXTENSIONS:
                continue
            file_tickers = parse_stock_list_file(path)
            if not file_tickers:
                continue
            log_debug(f"Loaded {len(file_tickers)} ticker(s) from {label} file: {path}")
            for ticker in file_tickers:
                key = ticker.upper()
                if key not in seen:
                    seen.add(key)
                    tickers.append(key)
    return tickers


def project_stock_list_directories() -> list[Path]:
    return [Path(__file__).resolve().parent]


def desktop_directories() -> list[Path]:
    candidates = [
        Path.home() / "Desktop",
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
    ]
    for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value) / "Desktop")

    desktops: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            desktops.append(resolved)
    return desktops


def parse_stock_list_file(path: Path) -> list[str]:
    if path.suffix.lower() not in DESKTOP_TICKER_EXTENSIONS:
        return []
    try:
        if path.stat().st_size > MAX_DESKTOP_TICKER_FILE_BYTES:
            log_debug(f"Skipping large ticker-list candidate: {path}")
            return []
        content = read_text_file(path)
    except OSError:
        return []
    return extract_tickers_from_stock_list_text(content, path.name)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def extract_tickers_from_stock_list_text(text: str, source_name: str = "") -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    header_tickers = extract_tickers_from_tabular_text(lines)
    if header_tickers:
        return unique_tickers(header_tickers)

    data_lines = [line for line in lines if not is_ignored_stock_list_line(line)]
    if not data_lines:
        return []

    tickers: list[str] = []
    candidate_lines = 0
    for line in data_lines:
        line_tickers = extract_tickers_from_line(line)
        if line_tickers:
            candidate_lines += 1
            tickers.extend(line_tickers)

    tickers = unique_tickers(tickers)
    if not tickers:
        return []

    filename_hint = has_stock_list_filename_hint(source_name)
    candidate_ratio = candidate_lines / max(len(data_lines), 1)
    looks_like_list = (
        (len(tickers) >= 2 and candidate_ratio >= 0.5)
        or (len(tickers) >= 3 and candidate_ratio >= 0.3)
        or (filename_hint and candidate_lines >= 1)
        or (len(data_lines) <= 3 and candidate_lines == len(data_lines))
    )
    return tickers if looks_like_list else []


def extract_tickers_from_tabular_text(lines: list[str]) -> list[str]:
    sample = "\n".join(lines[:20])
    full_text = "\n".join(lines)
    for delimiter in (",", "\t", ";", "|"):
        if delimiter not in sample:
            continue
        rows = list(csv.reader(io.StringIO(full_text), delimiter=delimiter))
        if not rows:
            continue
        header = [normalize_header_cell(cell) for cell in rows[0]]
        ticker_columns = [idx for idx, cell in enumerate(header) if cell in TICKER_LABELS]
        if not ticker_columns:
            continue
        tickers: list[str] = []
        for row in rows[1:]:
            for idx in ticker_columns:
                if idx < len(row):
                    ticker = normalize_ticker_token(row[idx])
                    if ticker:
                        tickers.append(ticker)
        if tickers:
            return tickers
    return []


def extract_tickers_from_line(line: str) -> list[str]:
    line = strip_inline_comment(line).strip()
    if not line:
        return []

    labeled = re.search(
        r"(?i)(?:\b(?:ticker|symbol|stock|code)\b|股票代码|证券代码|代码)\s*[:=]\s*([A-Z0-9.\-]+)",
        line,
    )
    if labeled:
        ticker = normalize_ticker_token(labeled.group(1))
        return [ticker] if ticker else []

    tokens = split_ticker_line(line)
    candidates = [normalize_ticker_token(token) for token in tokens]
    candidates = [ticker for ticker in candidates if ticker]
    if not candidates:
        return []

    if len(tokens) == 1:
        return candidates

    candidate_ratio = len(candidates) / len(tokens)
    if candidate_ratio >= 0.8:
        return candidates

    first = normalize_ticker_token(tokens[0])
    return [first] if first else []


def split_ticker_line(line: str) -> list[str]:
    line = line.replace("\ufeff", "")
    line = line.replace("，", ",").replace("；", ";").replace("｜", "|")
    parts = re.split(r"[\s,;\t|]+", line.strip())
    return [part for part in parts if part]


def normalize_ticker_token(token: str) -> str | None:
    token = token.strip().strip("\"'`[](){}")
    if "#" in token:
        prefix, value = token.rsplit("#", 1)
        if prefix.strip().isdigit():
            token = value
    if ":" in token:
        prefix, value = token.rsplit(":", 1)
        if prefix.strip().upper() in {"AMEX", "NASDAQ", "NYSE", "US"}:
            token = value
    token = token.upper()
    if token in TICKER_STOPWORDS or not TICKER_TOKEN_RE.fullmatch(token):
        return None
    return token


def normalize_header_cell(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().lower())


def strip_inline_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("#") or stripped.startswith("//"):
        return ""
    match = re.search(r"\s(?:#|//)", line)
    if match:
        return line[: match.start()]
    return line


def is_ignored_stock_list_line(line: str) -> bool:
    clean = line.strip()
    if not clean:
        return True
    if clean.startswith(("#", "//", "--")):
        return True
    return normalize_header_cell(clean) in TICKER_LABELS


def has_stock_list_filename_hint(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in STOCK_LIST_FILENAME_HINTS)


def unique_tickers(values: Iterable[str]) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = value.upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def download_ticker_mapping() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        content = download_text(TICKER_URL)
        for line in content.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2:
                mapping[parts[0].upper()] = parts[1]
    except Exception:
        pass
    return mapping or dict(FALLBACK_TICKER_MAP)


def clean_ticker_key(ticker: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", ticker.upper())


def normalize_cik(cik: str) -> str:
    stripped = cik.lstrip("0")
    return stripped or "0"


def find_cik_for_ticker(ticker: str, ticker_to_cik: dict[str, str]) -> str | None:
    clean_input = clean_ticker_key(ticker)
    if not clean_input:
        return None
    for source in (ticker_to_cik, FALLBACK_TICKER_MAP):
        for key, cik in source.items():
            if clean_ticker_key(key) == clean_input:
                return cik
    return None


def find_master_index(start_date: date, max_lookback_days: int) -> MasterIndex | None:
    candidates: list[tuple[int, date, str]] = []
    for offset in range(max_lookback_days):
        current = start_date - timedelta(days=offset)
        candidates.append((offset, current, master_index_url(current)))
    if not candidates:
        return None

    def fetch(candidate: tuple[int, date, str]) -> tuple[int, date, str] | None:
        offset, current, url = candidate
        try:
            content = download_text(url)
            if content.strip():
                log_debug(f"Using SEC index: {url}")
                return offset, current, content
        except Exception:
            pass
        return None

    max_workers = min(len(candidates), env_int("INDEX_WORKERS", DEFAULT_INDEX_WORKERS))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = [result for result in executor.map(fetch, candidates) if result]

    if results:
        results.sort(key=lambda item: item[0])
        found_date = results[0][1]
        return MasterIndex(found_date.strftime("%Y%m%d"), "".join(item[2] for item in results))
    return None


def master_index_url(index_date: date) -> str:
    quarter = (index_date.month - 1) // 3 + 1
    return (
        f"{SEC_BASE}edgar/daily-index/{index_date.year}/QTR{quarter}/"
        f"master.{index_date.strftime('%Y%m%d')}.idx"
    )


def download_text(url: str) -> str:
    headers = {
        "User-Agent": first_non_blank(os.getenv("SEC_USER_AGENT"), DEFAULT_SEC_USER_AGENT),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "From": first_non_blank(os.getenv("SEC_CONTACT_EMAIL"), DEFAULT_SEC_CONTACT_EMAIL),
    }
    last_exception: Exception | None = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_exception = RuntimeError(f"HTTP {exc.code} for {url} (attempt {attempt})")
            if exc.code in {403, 404}:
                raise last_exception
        except Exception as exc:
            last_exception = exc
        if attempt < 3:
            time.sleep(2 if isinstance(last_exception, RuntimeError) else 1)
    raise last_exception or RuntimeError(f"Failed to download {url} after 3 attempts")


def parse_master_idx(content: str | None, ciks: set[str]) -> list[str]:
    clean_ciks = {normalize_cik(cik) for cik in ciks}
    urls: dict[str, None] = {}
    if content is None:
        return []
    for line in content.splitlines():
        if not line.strip() or line.startswith("CIK|") or line.startswith("-----"):
            continue
        parts = line.split("|", 5)
        if len(parts) < 5:
            continue
        file_cik = normalize_cik(parts[0].strip())
        form_type = parts[2].strip()
        if not form_type.startswith("4"):
            continue
        if file_cik in clean_ciks:
            filename = parts[4].strip()
            if filename:
                urls[f"{SEC_BASE}{filename}"] = None
    return list(urls)


def fetch_form4_urls_from_edgar_browse(ciks: set[str], max_lookback_days: int) -> list[str]:
    urls: dict[str, None] = {}
    for cik in ciks:
        try:
            browse_url = (
                "https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik}&type=4&owner=include&count=100&output=atom"
            )
            atom_xml = download_text(browse_url)
            if atom_xml.strip():
                for url in parse_browse_edgar_atom(atom_xml, max_lookback_days):
                    urls[url] = None
        except Exception:
            print(f"Warning: browse-edgar fallback failed for CIK {cik}", file=sys.stderr)
    return list(urls)


def parse_browse_edgar_atom(atom_xml: str, max_lookback_days: int) -> list[str]:
    urls: dict[str, None] = {}
    threshold = date.today() - timedelta(days=max_lookback_days)
    entry_pattern = re.compile(r"<entry>(.*?)</entry>", re.IGNORECASE | re.DOTALL)
    date_pattern = re.compile(r"<filing-date>(.*?)</filing-date>", re.IGNORECASE | re.DOTALL)
    href_pattern = re.compile(r"<filing-href>(.*?)</filing-href>", re.IGNORECASE | re.DOTALL)
    for entry_match in entry_pattern.finditer(atom_xml):
        entry = entry_match.group(1)
        date_match = date_pattern.search(entry)
        href_match = href_pattern.search(entry)
        if not date_match or not href_match:
            continue
        try:
            filing_date = date.fromisoformat(date_match.group(1).strip())
            if filing_date < threshold:
                continue
            xml_url = find_form4_xml_url_from_index_page(href_match.group(1).strip())
            if xml_url:
                urls[xml_url] = None
        except Exception:
            pass
    return list(urls)


def find_form4_xml_url_from_index_page(index_url: str) -> str | None:
    try:
        html = download_text(index_url)
    except Exception:
        return None
    if not html.strip():
        return None
    pattern = re.compile(r'href="([^"]*?/form4\.xml)"', re.IGNORECASE)
    best_url = None
    for match in pattern.finditer(html):
        relative = match.group(1).strip()
        full_url = relative if relative.startswith("http") else f"https://www.sec.gov{relative}"
        if "xslf345" not in relative.lower():
            return full_url
        best_url = best_url or full_url
    return best_url


def parse_form4(
    xml: str,
    minimum_usd: int,
    cik_to_requested_ticker: dict[str, str],
) -> dict[str, list[AlertEntry]]:
    alerts: dict[str, list[AlertEntry]] = {}
    xml_payload = extract_xml_payload(xml)
    if not xml_payload.strip():
        log_debug("Skipping file: Could not extract valid XML payload.")
        return alerts

    root = ET.fromstring(xml_payload)
    doc = child(root, "ownershipDocument") or root
    issuer = child(doc, "issuer")
    if issuer is None:
        return alerts

    raw_cik = text_at(issuer, "issuerCik") or text_at(issuer, "issuerCIK") or "Unknown"
    normalized_cik = normalize_cik(raw_cik)
    ticker = cik_to_requested_ticker.get(
        normalized_cik, text_at(issuer, "issuerTradingSymbol") or "Unknown"
    )

    reporting_owner = child(doc, "reportingOwner")
    if reporting_owner is None or not is_officer_or_director(reporting_owner):
        log_debug(f"Skipping Form 4 for {ticker} - reporter is not an officer/director.")
        return alerts

    owner_name = text_at(reporting_owner, "reportingOwnerId.rptOwnerName") or "Unknown Owner"
    position = extract_position(reporting_owner)

    for table_name, tx_name in (
        ("nonDerivativeTable", "nonDerivativeTransaction"),
        ("derivativeTable", "derivativeTransaction"),
    ):
        table = child(doc, table_name)
        if table is None:
            log_debug(f"No {table_name} for {ticker}")
            continue
        transactions = children(table, tx_name)
        if not transactions:
            log_debug(f"No {tx_name} for {ticker}")
            continue
        for transaction in transactions:
            entry = process_transaction(transaction, owner_name, position, minimum_usd)
            if entry:
                alerts.setdefault(ticker, []).append(entry)

    alerts.setdefault(ticker, [])
    return alerts


def process_form4_urls(
    form4_urls: list[str],
    minimum_usd: int,
    cik_to_requested_ticker: dict[str, str],
) -> tuple[dict[str, list[AlertEntry]], int, int]:
    all_alerts: dict[str, list[AlertEntry]] = {}
    processed_count = 0
    failed_count = 0
    if not form4_urls:
        return all_alerts, processed_count, failed_count

    def process(url: str) -> tuple[str, dict[str, list[AlertEntry]] | None, Exception | None]:
        try:
            xml = download_text(url)
            log_debug(f"Processing Form 4 URL: {url}")
            return url, parse_form4(xml, minimum_usd, cik_to_requested_ticker), None
        except Exception as exc:
            return url, None, exc

    max_workers = min(len(form4_urls), env_int("FORM4_WORKERS", DEFAULT_FORM4_WORKERS))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for url, parsed, error in executor.map(process, form4_urls):
            if error:
                failed_count += 1
                print(f"Warning: failed to process Form 4 at {url} - {error}", file=sys.stderr)
                continue
            processed_count += 1
            for ticker, alerts in (parsed or {}).items():
                if alerts:
                    all_alerts.setdefault(ticker, []).extend(alerts)

    return all_alerts, processed_count, failed_count


def is_officer_or_director(reporting_owner: ET.Element) -> bool:
    relationship = child(reporting_owner, "reportingOwnerRelationship")
    if relationship is None:
        return False
    is_director = text_at(relationship, "isDirector") or ""
    is_officer = text_at(relationship, "isOfficer") or ""
    return is_director.lower() == "true" or is_director == "1" or is_officer.lower() == "true" or is_officer == "1"


def extract_position(reporting_owner: ET.Element) -> str:
    relationship = child(reporting_owner, "reportingOwnerRelationship")
    titles: list[str] = []
    saw_remarks = False
    if relationship is not None:
        for field in ("officerTitle", "directorTitle", "otherTitle"):
            value = text_at(relationship, field)
            if value:
                title = value.strip()
                if is_see_remarks(title):
                    saw_remarks = True
                else:
                    titles.append(title)
        if titles:
            return ", ".join(titles)
        if saw_remarks:
            return infer_position_from_relationship(relationship) or "See Remarks"
    for path in ("relationshipTitle", "reportingOwnerId.rptOwnerTitle"):
        value = text_at(reporting_owner, path)
        if value:
            title = value.strip()
            if is_see_remarks(title) and relationship is not None:
                return infer_position_from_relationship(relationship) or "See Remarks"
            return title
    return "Unknown Position"


def is_see_remarks(value: str) -> bool:
    return bool(re.search(r"\bsee remarks\b", value, re.IGNORECASE))


def infer_position_from_relationship(relationship: ET.Element) -> str | None:
    if relationship_flag(relationship, "isOfficer"):
        return "Officer"
    if relationship_flag(relationship, "isDirector"):
        return "Director"
    return None


def relationship_flag(relationship: ET.Element, field: str) -> bool:
    value = (text_at(relationship, field) or "").strip().lower()
    return value in {"1", "true", "yes"}


def process_transaction(
    transaction: ET.Element,
    owner_name: str,
    position: str,
    minimum_usd: int,
) -> AlertEntry | None:
    code = text_at(transaction, "transactionCoding.transactionCode") or ""
    if code not in {"P", "S"}:
        log_debug(f"Skipping transaction: code={code} (not P/S)")
        return None

    exercise_date_node = node_at(transaction, "exerciseDate")
    exercise_date = direct_text(exercise_date_node) if exercise_date_node is not None else ""
    if exercise_date:
        log_debug(f"Skipping transaction: code={code} has exerciseDate={exercise_date}")
        return None

    shares = extract_int(transaction, "transactionAmounts.transactionShares")
    price = extract_float(transaction, "transactionAmounts.transactionPricePerShare")
    if shares <= 0 or price <= 0:
        log_debug(f"Skipping transaction: code={code} shares={shares} price={price}")
        return None

    amount = shares * price
    if amount < minimum_usd:
        log_debug(f"Skipping transaction: code={code} amount={amount} < threshold={minimum_usd}")
        return None

    kind = "BUY" if code == "P" else "SELL"
    is_10b5_1 = (text_at(transaction, "transactionCoding.is10b51Transaction") or "").lower() == "true"
    transaction_date = extract_text(transaction, "transactionDate", "")
    if len(transaction_date) >= 10:
        transaction_date = transaction_date[:10]

    shares_owned_after = extract_int(
        transaction, "postTransactionAmounts.sharesOwnedFollowingTransaction"
    )
    if shares_owned_after <= 0:
        shares_owned_after = extract_int(transaction, "sharesOwnedFollowingTransaction")

    log_debug(
        f"Creating alert: {owner_name} {kind} {shares} shares at {price} "
        f"amount={amount} date={transaction_date} ownedAfter={shares_owned_after}"
    )
    return AlertEntry(
        owner_name,
        position,
        kind,
        shares,
        price,
        amount,
        is_10b5_1,
        transaction_date,
        shares_owned_after,
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(root: ET.Element | None, name: str) -> ET.Element | None:
    if root is None:
        return None
    for item in list(root):
        if local_name(item.tag) == name:
            return item
    return None


def children(root: ET.Element | None, name: str) -> list[ET.Element]:
    if root is None:
        return []
    return [item for item in list(root) if local_name(item.tag) == name]


def node_at(root: ET.Element | None, path: str) -> ET.Element | None:
    node = root
    for part in path.split("."):
        node = child(node, part)
        if node is None:
            return None
    return node


def direct_text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def text_at(root: ET.Element | None, path: str) -> str:
    return direct_text(node_at(root, path))


def value_text(node: ET.Element | None) -> str:
    direct = direct_text(node)
    if direct:
        return direct
    return direct_text(child(node, "value"))


def extract_int(root: ET.Element, path: str) -> int:
    return parse_number_as_int(value_text(node_at(root, path)))


def extract_float(root: ET.Element, path: str) -> float:
    return parse_number_as_float(value_text(node_at(root, path)))


def extract_text(root: ET.Element, path: str, fallback: str) -> str:
    value = value_text(node_at(root, path))
    return value if value else fallback


def parse_number_as_int(text: str) -> int:
    try:
        cleaned = re.sub(r"[^0-9.\-]", "", text or "")
        return int(float(cleaned)) if cleaned else 0
    except ValueError:
        return 0


def parse_number_as_float(text: str) -> float:
    try:
        cleaned = re.sub(r"[^0-9.\-]", "", text or "")
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def extract_xml_payload(raw_text: str | None) -> str:
    if raw_text is None:
        return ""
    clean_xml = ""
    xml_start = raw_text.find("<XML>")
    if xml_start >= 0:
        xml_end = raw_text.find("</XML>", xml_start)
        if xml_end > xml_start:
            clean_xml = raw_text[xml_start + 5 : xml_end]
    if not clean_xml.strip():
        match = re.search(
            r"<ownershipDocument[^>]*>.*?</ownershipDocument>",
            raw_text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            clean_xml = match.group(0)
    if not clean_xml.strip():
        return ""
    clean_xml = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", clean_xml)
    clean_xml = re.sub(r"&(?!(amp|apos|quot|lt|gt|#\d+);)", "&amp;", clean_xml)
    clean_xml = re.sub(r"</\s+", "</", clean_xml)
    clean_xml = re.sub(r"<\s+(?=[a-zA-Z_/?!])", "<", clean_xml)
    clean_xml = re.sub(r"<(?=[^a-zA-Z_/?!])", "&lt;", clean_xml)
    return clean_xml.strip()


def build_grouped_notification(
    alerts_by_ticker: dict[str, list[AlertEntry]], index_date: str
) -> str:
    lines: list[str] = []
    for group_index, (ticker, entries) in enumerate(sorted_ticker_alert_groups(alerts_by_ticker)):
        if group_index > 0:
            lines.extend(["", "---", ""])
        for entry_index, entry in enumerate(entries):
            if entry_index > 0:
                lines.append("")
            lines.extend(format_alert_block(ticker, entry))

    return "\n".join(lines).strip()


def sorted_ticker_alert_groups(
    alerts_by_ticker: dict[str, list[AlertEntry]],
) -> list[tuple[str, list[AlertEntry]]]:
    groups = [(ticker, sorted_alerts_for_ticker(alerts)) for ticker, alerts in alerts_by_ticker.items()]
    return sorted(
        groups,
        key=lambda item: (
            -max_buy_amount(item[1]),
            -max((entry.amount for entry in item[1]), default=0.0),
            item[0],
        ),
    )


def sorted_alerts_for_ticker(alerts: list[AlertEntry]) -> list[AlertEntry]:
    return sorted(
        alerts,
        key=lambda entry: (0 if entry.kind == "BUY" else 1, -entry.amount, entry.transaction_date),
    )


def max_buy_amount(alerts: list[AlertEntry]) -> float:
    return max((entry.amount for entry in alerts if entry.kind == "BUY"), default=0.0)


def format_alert_block(ticker: str, entry: AlertEntry) -> list[str]:
    tx_date = entry.transaction_date or "N/A"
    position = display_position(entry.position)
    amount = format_amount(entry.amount)
    plan = " · 10b5-1" if entry.is_10b5_1 else ""
    percent = format_holding_change_percent(entry)
    price = format_price(entry.price)
    indent = "\u3000  "
    detail = f"{indent}{tx_date}{plan}   {percent}@ {price}" if percent else f"{indent}{tx_date}{plan} @ {price}"

    if entry.kind == "BUY":
        return [
            markdown_hard_break(f"🔸 {ticker} · BUY · {amount} · {position}"),
            detail,
        ]

    return [
        markdown_hard_break(f"🔹 {ticker} · SELL · {amount} · {position}"),
        detail,
    ]


def markdown_hard_break(line: str) -> str:
    return f"{line}  "


def compact_display_text(value: str) -> str:
    compacted = re.sub(r"\s+", " ", value or "").strip()
    return compacted or "N/A"


def display_position(position: str) -> str:
    abbreviated = abbreviate_position(position)
    if abbreviated == "OFF":
        return "高管"
    if abbreviated == "DIR":
        return "董事"
    return abbreviated


def format_holding_change_percent(entry: AlertEntry) -> str:
    if entry.shares <= 0:
        return ""
    if entry.kind == "BUY":
        shares_before = entry.shares_owned_after - entry.shares
        if shares_before <= 0:
            return "NEW"
        percent = entry.shares / shares_before * 100
        sign = "+"
    else:
        shares_before = entry.shares_owned_after + entry.shares
        if shares_before <= 0:
            return ""
        percent = entry.shares / shares_before * 100
        sign = "-"
    if percent < 10:
        return f"{sign}{percent:.1f}%"
    return f"{sign}{int(percent + 0.5)}%"


def abbreviate_position(position: str) -> str:
    text = compact_display_text(position)
    if is_see_remarks(text):
        return "REM"
    if text == "N/A" or re.search(r"\b(unknown|not applicable|none)\b", text, re.IGNORECASE):
        return "N/A"

    normalized = text.upper()
    role_patterns = [
        ("CEO", r"\bCEO\b|CHIEF EXECUTIVE|PRESIDENT\s+(&|AND)\s+CEO"),
        ("CFO", r"\bCFO\b|CHIEF FINANCIAL"),
        ("COO", r"\bCOO\b|CHIEF OPERATING"),
        ("CTO", r"\bCTO\b|CHIEF TECHNOLOGY"),
        ("CIO", r"\bCIO\b|CHIEF INFORMATION|CHIEF INVESTMENT"),
        ("CMO", r"\bCMO\b|CHIEF MARKETING"),
        ("CLO", r"\bCLO\b|CHIEF LEGAL"),
        ("CHRO", r"\bCHRO\b|CHIEF HUMAN|HUMAN RESOURCES"),
        ("CAO", r"\bCAO\b|CHIEF ACCOUNTING|CHIEF ADMINISTRATIVE"),
        ("CCO", r"\bCCO\b|CHIEF COMPLIANCE|CHIEF COMMERCIAL"),
        ("CRO", r"\bCRO\b|CHIEF REVENUE|CHIEF RISK"),
        ("CSO", r"\bCSO\b|CHIEF STRATEGY|CHIEF SCIENTIFIC"),
        ("CDO", r"\bCDO\b|CHIEF DATA|CHIEF DIGITAL|CHIEF DEVELOPMENT"),
        ("CPO", r"\bCPO\b|CHIEF PRODUCT|CHIEF PEOPLE"),
        ("GP", r"\bGP\b|\bGROUP PRESIDENT\b"),
        ("COCH", r"\bCOCH\b|\bCO-?CHAIR(MAN|WOMAN)?\b"),
        ("EVP", r"\bEVP\b|EXECUTIVE VICE PRESIDENT"),
        ("SVP", r"\bSVP\b|SENIOR VICE PRESIDENT"),
        ("VP", r"\bVP\b|VICE PRESIDENT"),
        ("PRES", r"\bPRES\b|\bPRESIDENT\b"),
        ("CHAIR", r"\bCHAIR(MAN|WOMAN)?\b"),
        ("DIR", r"\bDIR\b|\bDIRECTOR\b"),
        ("OFF", r"\bOFF\b|\bOFFICER\b"),
    ]
    for label, pattern in role_patterns:
        if re.search(pattern, normalized):
            return label[:5]

    words = re.findall(r"[A-Z0-9]+", normalized)
    stop_words = {
        "A",
        "AN",
        "AND",
        "AS",
        "AT",
        "CO",
        "COMPANY",
        "CORP",
        "CORPORATE",
        "INC",
        "LLC",
        "LP",
        "LTD",
        "OF",
        "THE",
    }
    initials = "".join(word[0] for word in words if word not in stop_words)
    if initials:
        return initials[:5]

    compacted = re.sub(r"[^A-Z0-9]", "", normalized)
    return (compacted or "N/A")[:5]


def format_number(num: int) -> str:
    if num >= 1_000_000:
        return f"{num / 1_000_000.0:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000.0:.1f}K"
    return str(num)


def format_amount(amount: float) -> str:
    units = [(1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for index, (scale, suffix) in enumerate(units):
        if amount >= scale:
            scaled = amount / scale
            if scaled >= 999.5 and index > 0:
                scale, suffix = units[index - 1]
                scaled = amount / scale
            return f"${format_significant(scaled, 3)}{suffix}"
    return f"${format_significant(amount, 3)}"


def format_price(price: float) -> str:
    return f"${format_significant(price, 4)}"


def format_significant(value: float, digits: int) -> str:
    if value == 0:
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    decimals = max(digits - magnitude - 1, 0)
    rounded = round(value, decimals)
    if decimals == 0:
        return f"{rounded:.0f}"
    return f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")


def build_missing_notification(tickers: list[str], reason: str) -> str:
    lines = ["🔔 Insider Alerts", ""]
    for ticker in tickers:
        lines.extend([f"▶ {ticker}", f"  {reason}", ""])
    return "\n".join(lines).strip()


def send_notification(message: str) -> bool:
    ding_url = os.getenv("DING_WEBHOOK_URL")
    if ding_url and ding_url.strip():
        return send_dingtalk_webhook(
            ding_url, os.getenv("DING_WEBHOOK_SIGN"), "Insider Alert", message
        )

    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not discord_url or not discord_url.strip():
        return False
    return send_discord_webhook(discord_url, "Insider Alert", message)


def send_error_notification(error_message: str) -> None:
    ding_url = os.getenv("DING_WEBHOOK_URL")
    if ding_url and ding_url.strip():
        send_dingtalk_webhook(
            ding_url, os.getenv("DING_WEBHOOK_SIGN"), "Insider Bot Error", error_message
        )
        return

    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_url and discord_url.strip():
        send_discord_webhook(discord_url, "Insider Bot Error", error_message)


def send_dingtalk_webhook(
    webhook_url: str, secret: str | None, title: str, message: str
) -> bool:
    try:
        return send_dingtalk_messages(webhook_url, secret, title, message)
    except Exception as exc:
        print(f"Warning: failed to send DingTalk notification: {exc}", file=sys.stderr)
        return False


def send_dingtalk_messages(
    webhook_url: str,
    secret: str | None,
    title: str,
    message: str,
    max_payload_bytes: int = DINGTALK_SAFE_BODY_BYTES,
) -> bool:
    chunks = split_dingtalk_message(title, message, max_payload_bytes)
    success = True
    for chunk in chunks:
        if not send_single_dingtalk_message(webhook_url, secret, title, chunk):
            success = False
    return success


def send_single_dingtalk_message(
    webhook_url: str, secret: str | None, title: str, message: str
) -> bool:
    signed_url = build_dingtalk_url(webhook_url, secret)
    payload = build_dingtalk_payload(title, message)
    status, body = post_json(signed_url, payload)
    success = 200 <= status < 300 and '"errcode":0' in body.replace(" ", "")
    if not success:
        print(
            f"Warning: DingTalk notification failed. status={status} body={body}",
            file=sys.stderr,
        )
    return success


def split_dingtalk_message(
    title: str,
    message: str,
    max_payload_bytes: int = DINGTALK_SAFE_BODY_BYTES,
    timestamp: str | None = None,
) -> list[str]:
    timestamp = timestamp or current_dingtalk_segment_timestamp()
    message = message.strip()
    if dingtalk_segment_payload_byte_size(title, message, timestamp) <= max_payload_bytes:
        return [format_dingtalk_segment(message, timestamp)]

    chunks: list[str] = []
    current = ""
    for block in split_markdown_blocks(message):
        for part in split_dingtalk_block(title, block, max_payload_bytes, timestamp):
            candidate = append_markdown_block(current, part)
            if current and dingtalk_segment_payload_byte_size(title, candidate, timestamp) > max_payload_bytes:
                chunks.append(current)
                current = part
            else:
                current = candidate

    if current:
        chunks.append(current)
    chunks = chunks or [message]
    total = len(chunks)
    return [
        format_dingtalk_segment(chunk, timestamp, index, total)
        for index, chunk in enumerate(chunks, 1)
    ]


def split_markdown_blocks(message: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", message) if block.strip()]


def append_markdown_block(current: str, block: str) -> str:
    return block if not current else f"{current}\n\n{block}"


def split_dingtalk_block(
    title: str, block: str, max_payload_bytes: int, timestamp: str
) -> list[str]:
    if dingtalk_segment_payload_byte_size(title, block, timestamp) <= max_payload_bytes:
        return [block]

    parts: list[str] = []
    current = ""
    for line in block.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if dingtalk_segment_payload_byte_size(title, candidate, timestamp) <= max_payload_bytes:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        if dingtalk_segment_payload_byte_size(title, line, timestamp) <= max_payload_bytes:
            current = line
        else:
            parts.extend(split_dingtalk_line(title, line, max_payload_bytes, timestamp))

    if current:
        parts.append(current)
    return parts


def split_dingtalk_line(
    title: str, line: str, max_payload_bytes: int, timestamp: str
) -> list[str]:
    parts: list[str] = []
    remaining = line
    while remaining:
        low, high = 1, len(remaining)
        best = 0
        while low <= high:
            mid = (low + high) // 2
            candidate = remaining[:mid]
            if dingtalk_segment_payload_byte_size(title, candidate, timestamp) <= max_payload_bytes:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        if best <= 0:
            raise ValueError("DingTalk payload byte limit is too small for any content")
        parts.append(remaining[:best])
        remaining = remaining[best:]
    return parts


def current_dingtalk_segment_timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def format_dingtalk_segment(
    message: str, timestamp: str, segment_index: int = 1, segment_total: int = 1
) -> str:
    segment_label = f"  ({segment_index}/{segment_total})" if segment_total > 1 else ""
    return f"---\n---\n# ⏰{segment_label}\n# {timestamp}\n---\n\n{message.strip()}"


def build_dingtalk_payload(title: str, message: str) -> dict:
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": message},
    }


def dingtalk_payload_byte_size(title: str, message: str) -> int:
    return len(json.dumps(build_dingtalk_payload(title, message), ensure_ascii=False).encode("utf-8"))


def dingtalk_segment_payload_byte_size(title: str, message: str, timestamp: str) -> int:
    return dingtalk_payload_byte_size(title, format_dingtalk_segment(message, timestamp))


def build_dingtalk_url(
    webhook_url: str, secret: str | None, timestamp: int | None = None
) -> str:
    if not secret or not secret.strip():
        return webhook_url
    timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode())
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={timestamp}&sign={sign}"


def send_discord_webhook(webhook_url: str, title: str, message: str) -> bool:
    try:
        return send_discord_messages(webhook_url, title, message)
    except Exception as exc:
        print(f"Warning: failed to send Discord notification: {exc}", file=sys.stderr)
        return False


def send_discord_messages(webhook_url: str, title: str, message: str) -> bool:
    full_body = message
    if json_escaped_length(full_body) <= 2000:
        return send_single_discord_message(webhook_url, full_body)

    success = True
    chunk = ""
    lines = full_body.splitlines(keepends=True)
    for line in lines:
        candidate = chunk + line
        if json_escaped_length(candidate) <= 2000:
            chunk = candidate
            continue
        if chunk and not send_single_discord_message(webhook_url, chunk):
            success = False
        chunk = line
    if chunk and not send_single_discord_message(webhook_url, chunk):
        success = False
    return success


def json_escaped_length(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False)[1:-1])


def send_single_discord_message(webhook_url: str, content: str) -> bool:
    try:
        status, _ = post_json(webhook_url, {"content": content})
        return 200 <= status < 300
    except Exception as exc:
        print(f"Warning: failed to send single Discord message: {exc}", file=sys.stderr)
        return False


def post_json(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


if __name__ == "__main__":
    raise SystemExit(main())
