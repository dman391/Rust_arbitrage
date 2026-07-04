#!/usr/bin/env python3
"""
skinport_to_steam_buy_order_only.py

Rust Skinport -> Steam Wallet token scanner.

What changed vs the larger closed-loop scanner:
  - This ONLY evaluates the first leg: buy ONE tradable item on Skinport,
    then sell it immediately on Steam to the current highest buy order.
  - It does NOT use Steam lowest ask / median sale price as the Steam sale price.
  - It uses the buy-order column on the Steam market page: "requests to buy at ...".
  - It can also estimate the second leg: spend Steam Wallet on a Steam lowest listing,
    then sell on Skinport for real-currency cash after Skinport seller fee.

Important:
  - This is a scanner/ranker only. It does not buy or sell anything.
  - Steam fee rounding is approximated. Always verify on Steam's sell confirmation screen.
  - Skinport public API does not support NZD, so use a supported Skinport currency
    such as AUD or USD and set --skinport-to-steam-fx to convert into Steam currency.

Example New Zealand workflow:
  python skinport_to_steam_buy_order_only_random.py --skinport-currency AUD --steam-currency NZD --skinport-to-steam-fx 1.10 --max-candidates 120 --min-net-roi 0.20

Outputs:
  skinport_to_steam_buy_order_tokens.csv
  steam_to_skinport_cashout.csv
  closed_loop_skinport_steam_skinport.csv
  pooled_first_leg_selected.csv
  pooled_closed_loop_summary.csv
  call_log_buy_order_only.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import html as html_lib
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests


STEAM_CURRENCIES = {
    "USD": 1,
    "GBP": 2,
    "EUR": 3,
    "CHF": 4,
    "RUB": 5,
    "PLN": 6,
    "BRL": 7,
    "JPY": 8,
    "NOK": 9,
    "IDR": 10,
    "MYR": 11,
    "PHP": 12,
    "SGD": 13,
    "THB": 14,
    "VND": 15,
    "KRW": 16,
    "TRY": 17,
    "UAH": 18,
    "MXN": 19,
    "CAD": 20,
    "AUD": 21,
    "NZD": 22,
    "CNY": 23,
    "INR": 24,
    "CLP": 25,
    "PEN": 26,
    "COP": 27,
    "ZAR": 28,
    "HKD": 29,
    "TWD": 30,
    "SAR": 31,
    "AED": 32,
    "ARS": 34,
    "ILS": 35,
    "KZT": 37,
    "KWD": 38,
    "QAR": 39,
    "CRC": 40,
    "UYU": 41,
}

# Skinport public /v1/items pricing currencies. NZD is not supported by Skinport.
SKINPORT_CURRENCIES = {
    "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR",
    "GBP", "HRK", "NOK", "PLN", "RUB", "SEK", "TRY", "USD",
}

RUST_APP_ID = 252490


@dataclass
class Config:
    app_id: int = RUST_APP_ID
    skinport_currency: str = "AUD"
    steam_currency: str = "NZD"
    # 1 Skinport currency unit = X Steam currency units. Example: 1 AUD = 1.10 NZD.
    skinport_to_steam_fx: float = 1.10

    # Used as the single-item max price filter, and also as the total pool budget
    # when --pool-budget is left as 0.
    budget_skinport_currency: float = 500.0
    # Total real cash budget to use when building the pooled first leg.
    # 0 means use budget_skinport_currency.
    pool_budget_skinport_currency: float = 0.0
    # Cashout-only simulator inputs. These let you skip scanning the first leg and ask:
    # "If I start with X Skinport cash and already achieved Y% Steam-wallet ROI,
    # can I cash out profitably through Skinport?"
    simulated_initial_skinport_cash: float = 0.0
    assumed_first_leg_roi: float = 0.0
    max_candidates: int = 500
    # Mode:
    #   first-leg   = current Skinport cash -> Steam wallet scanner only
    #   cashout     = Steam wallet -> Skinport cash scanner only
    #   closed-loop = first-leg plus best Steam -> Skinport cashout recommendation
    mode: str = "closed-loop"

    # Only keep first-leg rows with net ROI >= this value after Steam fees. 0.20 = 20%.
    min_net_roi: float = 0.20

    # Second leg / cashout settings.
    steam_wallet_balance: float = 0.0
    cashout_candidates: int = 120
    skinport_seller_fee_rate: float = 0.08
    min_cashout_ratio: float = 0.0
    min_skinport_sales_24h: int = 0
    # For pooled closed-loop, "biggest-spend" tries to use as much Steam wallet as possible.
    cashout_ranking: str = "biggest-spend"
    # Optional: only consider cashout skins that spend at least this fraction of the
    # pooled Steam wallet. 0 = no minimum. Example: 0.90 means spend 90%+ of wallet.
    pool_min_wallet_spend_ratio: float = 0.0
    # Candidate selection:
    #   random = different filtered Skinport skins each run
    #   top    = old behaviour: highest discount/quantity candidates first
    #   mixed  = half top candidates, half random candidates
    candidate_selection: str = "random"
    random_seed: Optional[int] = None

    # Optional SCMM candidate seeding. This does NOT replace the maths in this
    # scanner. It only uses SCMM as a hint list so Steam calls are spent on
    # skins that SCMM already thinks may be interesting. Keep this cached so
    # SCMM is not hit every run.
    use_scmm_candidates: bool = False
    scmm_deals_url: str = "https://rust.scmm.app/market-deals"
    # If you find the exact endpoint in your browser Network tab, paste it here
    # with --scmm-api-url. The bot also tries a small list of common API paths.
    scmm_api_url: str = ""
    scmm_candidate_limit: int = 120
    scmm_cache_seconds: int = 600
    scmm_cache_file: str = "scmm_candidates_cache.json"

    min_skinport_quantity: int = 1
    min_skinport_price: float = 0.50
    max_skinport_price: float = 500.00
    min_highest_buy_order: float = 0.01

    # Steam fees. For Rust, total is commonly around 15% made of Steam + publisher fee.
    # Steam does cent rounding/floors, so this is an estimate.
    steam_fee_rate: float = 0.05
    publisher_fee_rate: float = 0.10
    steam_min_fee: float = 0.01
    publisher_min_fee: float = 0.01

    steam_country: str = "NZ"
    steam_language: str = "english"
    steam_delay_seconds: float = 11.0
    steam_delay_jitter_seconds: float = 3.0
    steam_429_sleep_seconds: float = 120.0
    steam_max_retries: int = 2
    request_timeout_seconds: float = 25.0

    output_csv: str = "skinport_to_steam_buy_order_tokens.csv"
    cashout_output_csv: str = "steam_to_skinport_cashout.csv"
    closed_loop_output_csv: str = "closed_loop_skinport_steam_skinport.csv"
    pool_selected_output_csv: str = "pooled_first_leg_selected.csv"
    pool_summary_output_csv: str = "pooled_closed_loop_summary.csv"
    simulated_cashout_output_csv: str = "simulated_cashout_from_roi.csv"
    call_log_json: str = "call_log_buy_order_only.json"
    debug_dir: str = "steam_debug_pages"
    steam_cookie: str = ""
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class CallCounter:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.started_at = time.time()

    def add(self, site: str) -> None:
        self.calls[site] = self.calls.get(site, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at_unix": self.started_at,
            "finished_at_unix": time.time(),
            "elapsed_seconds": round(time.time() - self.started_at, 2),
            "calls_by_site": dict(sorted(self.calls.items())),
            "total_calls": sum(self.calls.values()),
        }


def money_to_float(value: Any) -> Optional[float]:
    """Parse strings like '$1,234.56', 'NZ$ 2.50', '1,23€'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)

    s = str(value).strip()
    if not s:
        return None

    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None

    if "," in s and "." in s:
        # Last separator is probably the decimal separator.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s and "." not in s:
        if re.search(r",\d{1,2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return None


def parse_volume(value: Any) -> int:
    if value is None:
        return 0
    s = re.sub(r"[^0-9]", "", str(value))
    return int(s) if s else 0


def to_cents(amount: float) -> int:
    return int(round(amount * 100))


def from_cents(cents: int) -> float:
    return round(cents / 100.0, 2)


def steam_buyer_total_from_seller_net(
    seller_net: float,
    steam_fee_rate: float,
    publisher_fee_rate: float,
    steam_min_fee: float,
    publisher_min_fee: float,
) -> float:
    """Approximate buyer total from seller net using fee floors and cent rounding."""
    net_c = to_cents(seller_net)
    steam_fee_c = max(math.ceil(net_c * steam_fee_rate), to_cents(steam_min_fee))
    publisher_fee_c = max(math.ceil(net_c * publisher_fee_rate), to_cents(publisher_min_fee))
    return from_cents(net_c + steam_fee_c + publisher_fee_c)


def steam_seller_net_from_buyer_total(buyer_total: float, cfg: Config) -> float:
    """Approximate how much Steam Wallet you receive when buyer pays buyer_total."""
    buyer_c = to_cents(buyer_total)
    for net_c in range(buyer_c, -1, -1):
        total = steam_buyer_total_from_seller_net(
            from_cents(net_c),
            cfg.steam_fee_rate,
            cfg.publisher_fee_rate,
            cfg.steam_min_fee,
            cfg.publisher_min_fee,
        )
        if to_cents(total) <= buyer_c:
            return from_cents(net_c)
    return 0.0


def decode_skinport_json_response(r: requests.Response) -> Any:
    """Return JSON from Skinport, with manual Brotli fallback."""
    try:
        return r.json()
    except requests.exceptions.JSONDecodeError as exc:
        content_encoding = (r.headers.get("Content-Encoding") or "").lower()
        if "br" in content_encoding:
            try:
                import brotli  # type: ignore
            except ImportError as import_exc:
                raise RuntimeError(
                    "Skinport returned Brotli-compressed JSON, but Brotli support is not installed. "
                    "Run: python -m pip install brotli brotlicffi"
                ) from import_exc
            try:
                return json.loads(brotli.decompress(r.content).decode("utf-8"))
            except Exception as br_exc:
                raise RuntimeError("Could not decode Skinport Brotli JSON.") from br_exc
        raise RuntimeError(f"Skinport did not return readable JSON: {r.text[:300]!r}") from exc


def fetch_skinport_items(session: requests.Session, cfg: Config, calls: CallCounter) -> list[dict[str, Any]]:
    url = "https://api.skinport.com/v1/items"
    params = {
        "app_id": cfg.app_id,
        "currency": cfg.skinport_currency,
        "tradable": 1,
    }
    headers = {
        "Accept-Encoding": "br",
        "User-Agent": cfg.user_agent,
    }
    calls.add("skinport_items")
    r = session.get(url, params=params, headers=headers, timeout=cfg.request_timeout_seconds)
    r.raise_for_status()
    data = decode_skinport_json_response(r)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Skinport /v1/items response: {str(data)[:200]}")
    return data



SCMM_COMMON_CANDIDATE_URLS = [
    "https://rust.scmm.app/api/market-deals",
    "https://rust.scmm.app/api/market/deals",
    "https://rust.scmm.app/api/MarketDeals",
    "https://rust.scmm.app/api/marketdeals",
    "https://rust.scmm.app/api/deals",
    "https://rust.scmm.app/api/v1/market-deals",
    "https://rust.scmm.app/api/v1/deals",
]


def clean_scmm_market_name(value: Any) -> Optional[str]:
    """Return a plausible Steam market hash name from an SCMM field/string."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = html_lib.unescape(text)
    # Decode Steam market listing URL paths if a URL snuck into the JSON/HTML.
    m = re.search(r"/market/listings/\d+/([^?#\"'<>]+)", text)
    if m:
        from urllib.parse import unquote
        text = unquote(m.group(1))
    text = text.replace("\\/", "/")
    text = re.sub(r"\s+", " ", text).strip()
    # Avoid obvious non-item strings. Real Rust market names can contain spaces,
    # hyphens, apostrophes, parentheses, and vertical bars, so do not over-filter.
    if len(text) < 3 or len(text) > 160:
        return None
    lowered = text.lower()
    bad_bits = [
        "http://", "https://", "javascript:", "loading", "market deals",
        "cheapest offers", "profitable flips", "steam community market manager",
    ]
    if any(bit in lowered for bit in bad_bits):
        return None
    return text


def extract_scmm_candidate_names_from_json(data: Any) -> set[str]:
    """Recursively pull item names from flexible SCMM/market-deal JSON."""
    names: set[str] = set()
    useful_key_bits = (
        "market_hash_name", "markethashname", "market_hash", "hash_name",
        "item_name", "itemname", "marketname", "market_name", "name",
    )

    def walk(obj: Any, parent_key: str = "") -> None:
        if isinstance(obj, dict):
            for key, val in obj.items():
                key_s = str(key)
                key_l = key_s.lower().replace("-", "_")
                compact = key_l.replace("_", "")
                if isinstance(val, str) and (
                    key_l in useful_key_bits
                    or compact in useful_key_bits
                    or "market_hash" in key_l
                    or "item_name" in key_l
                    or key_l in {"name", "title"}
                ):
                    cleaned = clean_scmm_market_name(val)
                    if cleaned:
                        names.add(cleaned)
                walk(val, key_s)
        elif isinstance(obj, list):
            for val in obj:
                walk(val, parent_key)

    walk(data)
    return names


def extract_scmm_candidate_names_from_html(html: str) -> set[str]:
    """Best-effort extraction when the market-deals page embeds data in HTML."""
    names: set[str] = set()
    text = html or ""

    # Steam listing URLs embedded in the page or serialized data.
    for m in re.finditer(r"/market/listings/\d+/([^?#\"'<>]+)", text):
        cleaned = clean_scmm_market_name(m.group(0))
        if cleaned:
            names.add(cleaned)

    # Common JSON-ish fields if the Blazor app serializes them into the page.
    patterns = [
        r'"(?:market_hash_name|marketHashName|item_name|itemName|marketName|name)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
        r"'(?:market_hash_name|marketHashName|item_name|itemName|marketName|name)'\s*:\s*'([^']+)'",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            raw = m.group(1)
            try:
                raw = bytes(raw, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            cleaned = clean_scmm_market_name(raw)
            if cleaned:
                names.add(cleaned)

    # JSON script blocks, if any.
    for m in re.finditer(r"(?is)<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>", text):
        try:
            data = json.loads(html_lib.unescape(m.group(1)).strip())
        except Exception:
            continue
        names.update(extract_scmm_candidate_names_from_json(data))

    return names


def load_scmm_cached_candidates(cfg: Config) -> Optional[set[str]]:
    path = Path(cfg.scmm_cache_file)
    if cfg.scmm_cache_seconds <= 0 or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(payload.get("saved_at_unix") or 0)
        if time.time() - saved_at > cfg.scmm_cache_seconds:
            return None
        names = payload.get("names") or []
        if isinstance(names, list):
            return {str(n) for n in names if clean_scmm_market_name(n)}
    except Exception:
        return None
    return None


def save_scmm_cached_candidates(cfg: Config, names: set[str], source: str) -> None:
    if cfg.scmm_cache_seconds <= 0:
        return
    payload = {
        "saved_at_unix": time.time(),
        "source": source,
        "count": len(names),
        "names": sorted(names),
    }
    try:
        Path(cfg.scmm_cache_file).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Warning: could not write SCMM cache {cfg.scmm_cache_file}: {exc}", file=sys.stderr)


def fetch_scmm_candidate_names(session: requests.Session, cfg: Config, calls: CallCounter) -> set[str]:
    """Fetch SCMM deal names to use as a candidate hint list.

    SCMM is only used to choose better skins to check first. Final ROI/profit is
    still calculated from Skinport + Steam data in this script.
    """
    if not cfg.use_scmm_candidates:
        return set()

    cached = load_scmm_cached_candidates(cfg)
    if cached is not None:
        limited = set(list(cached)[: max(0, int(cfg.scmm_candidate_limit))])
        print(f"Loaded {len(limited)} SCMM candidate names from cache: {cfg.scmm_cache_file}")
        return limited

    urls: list[str] = []
    if cfg.scmm_api_url.strip():
        urls.append(cfg.scmm_api_url.strip())
    urls.extend(SCMM_COMMON_CANDIDATE_URLS)
    if cfg.scmm_deals_url.strip():
        urls.append(cfg.scmm_deals_url.strip())

    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Referer": "https://rust.scmm.app/market-deals",
    }
    all_names: set[str] = set()
    source_used = ""

    # Try a small endpoint list. Stop as soon as one useful source returns names.
    for url in dict.fromkeys(urls):
        try:
            calls.add("scmm_candidates")
            r = session.get(url, headers=headers, timeout=cfg.request_timeout_seconds)
        except Exception as exc:
            print(f"SCMM candidate source failed: {url} ({exc})")
            continue

        if r.status_code in (404, 405):
            continue
        if r.status_code == 429:
            print(f"SCMM returned HTTP 429 for {url}; using no SCMM hints this run.")
            break
        if not (200 <= r.status_code < 300):
            print(f"SCMM candidate source HTTP {r.status_code}: {url}")
            continue

        ctype = (r.headers.get("Content-Type") or "").lower()
        names: set[str] = set()
        if "json" in ctype or r.text.lstrip().startswith(("{", "[")):
            try:
                names = extract_scmm_candidate_names_from_json(r.json())
            except Exception:
                names = extract_scmm_candidate_names_from_html(r.text)
        else:
            names = extract_scmm_candidate_names_from_html(r.text)

        if names:
            all_names = names
            source_used = url
            break

    if cfg.scmm_candidate_limit > 0:
        all_names = set(sorted(all_names)[: int(cfg.scmm_candidate_limit)])
    if all_names:
        save_scmm_cached_candidates(cfg, all_names, source_used)
        print(f"SCMM candidate hints loaded: {len(all_names)} names from {source_used}")
    else:
        print("SCMM candidate hints loaded: 0 names. The page may require a browser/SignalR endpoint; use --scmm-api-url with the Network-tab JSON endpoint if needed.")
    return all_names


def preselect_skinport_candidates(items: list[dict[str, Any]], cfg: Config, scmm_names: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Filter Skinport rows, then choose which skins to check on Steam.

    The old script always checked the same top discounted rows first. This version
    can randomize the chosen candidates so each run explores different skins.
    The output rows are still sorted by ROI after Steam data is collected.
    """
    scmm_lookup = {str(n).lower(): str(n) for n in (scmm_names or set())}
    scmm_lookup = {str(n).lower(): str(n) for n in (scmm_names or set())}
    candidates: list[dict[str, Any]] = []
    for item in items:
        name = item.get("market_hash_name")
        min_price = money_to_float(item.get("min_price"))
        qty = int(item.get("quantity") or 0)
        if not name or min_price is None:
            continue
        if qty < cfg.min_skinport_quantity:
            continue
        if min_price > cfg.budget_skinport_currency:
            continue
        if not (cfg.min_skinport_price <= min_price <= cfg.max_skinport_price):
            continue

        suggested = money_to_float(item.get("suggested_price")) or min_price
        discount = (suggested - min_price) / suggested if suggested > 0 else 0.0
        enriched = dict(item)
        enriched["_min_price_float"] = float(min_price)
        enriched["_discount_vs_suggested"] = float(discount)
        enriched["_scmm_candidate_hint"] = str(name).lower() in scmm_lookup
        candidates.append(enriched)

    candidates.sort(
        key=lambda x: (
            1 if x.get("_scmm_candidate_hint") else 0,
            x.get("_discount_vs_suggested", 0.0),
            math.log1p(int(x.get("quantity") or 0)),
            -float(x.get("_min_price_float") or 0.0),
        ),
        reverse=True,
    )

    limit = max(0, int(cfg.max_candidates))
    if limit == 0 or not candidates:
        return []

    mode = (cfg.candidate_selection or "random").lower().strip()
    rng = random.Random(cfg.random_seed)

    hinted = [x for x in candidates if x.get("_scmm_candidate_hint")]
    hinted_names = {str(x.get("market_hash_name")) for x in hinted}
    non_hinted = [x for x in candidates if str(x.get("market_hash_name")) not in hinted_names]

    if mode == "top":
        return candidates[:limit]

    if mode == "mixed":
        top_count = min(len(candidates), max(1, limit // 2))
        selected = candidates[:top_count]
        selected_names = {str(x.get("market_hash_name")) for x in selected}
        remaining = [x for x in candidates[top_count:] if str(x.get("market_hash_name")) not in selected_names]
        random_count = max(0, limit - len(selected))
        if random_count > 0:
            selected.extend(rng.sample(remaining, k=min(random_count, len(remaining))))
        rng.shuffle(selected)
        return selected[:limit]

    if mode != "random":
        print(f"Unknown candidate selection mode {cfg.candidate_selection!r}; using random.", file=sys.stderr)

    # Random mode still preserves all SCMM-hinted names first, then fills the
    # remaining slots randomly. This makes SCMM useful without losing exploration.
    selected = hinted[:limit]
    remaining_slots = max(0, limit - len(selected))
    if remaining_slots > 0:
        selected.extend(rng.sample(non_hinted, k=min(remaining_slots, len(non_hinted))))
    rng.shuffle(selected)
    return selected[:limit]


def steam_market_link(app_id: int, market_hash_name: str) -> str:
    return f"https://steamcommunity.com/market/listings/{app_id}/{quote(market_hash_name, safe='')}"


def safe_filename(name: str, max_len: int = 120) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:max_len].strip("_") or "item"


def steam_headers(cfg: Config, referer: str = "https://steamcommunity.com/market/") -> dict[str, str]:
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
    }
    if cfg.steam_cookie:
        headers["Cookie"] = cfg.steam_cookie
    return headers


def fetch_steam_listing_page(session: requests.Session, market_hash_name: str, cfg: Config, calls: CallCounter) -> str:
    """Fetch a Steam market listing page.

    Steam has recently rolled out beta market listing pages. Some beta HTML no longer
    exposes item_nameid, so this function tries several page variants and cookies that
    tend to return the older source where Market_LoadOrderSpread(id) is present.
    """
    base_url = steam_market_link(cfg.app_id, market_hash_name)

    # Prime common cookies. They are harmless if ignored by Steam.
    session.cookies.set("Steam_Language", cfg.steam_language, domain="steamcommunity.com")
    session.cookies.set("timezoneOffset", "43200,0", domain="steamcommunity.com")

    variants = [
        base_url,
        base_url + "?l=english",
        base_url + "?l=english&legacy=1",
        base_url + "?l=english&force_legacy=1",
        base_url + "?l=english&beta=0",
    ]

    last_text = ""
    last_status = None
    for url in variants:
        headers = steam_headers(cfg)
        calls.add("steam_listing_page")
        r = session.get(url, headers=headers, timeout=cfg.request_timeout_seconds)
        last_status = r.status_code
        if r.status_code == 429:
            raise RuntimeError("HTTP 429 from Steam listing page")
        r.raise_for_status()
        text = r.text or ""
        last_text = text
        if extract_item_nameid(text):
            return text

        # If Steam returns a generic community/login/captcha page, trying variants is unlikely
        # to help, but we still continue once or twice for clarity.
        lower = text.lower()
        if "captcha" in lower or "access denied" in lower:
            break

    # Save the failed HTML so you can inspect exactly what Steam returned.
    dbg_dir = Path(cfg.debug_dir)
    dbg_dir.mkdir(parents=True, exist_ok=True)
    dbg_path = dbg_dir / f"{safe_filename(market_hash_name)}.html"
    dbg_path.write_text(last_text, encoding="utf-8", errors="replace")

    # New/beta Steam market pages often no longer expose item_nameid, but they can
    # still include the visible buy-order table directly in the HTML. Return the
    # page so fetch_steam_highest_buy_order() can fall back to parsing that table.
    return last_text


def extract_item_nameid(listing_html: str) -> Optional[str]:
    """Extract Steam's numeric item_nameid from old or partially escaped listing HTML."""
    if not listing_html:
        return None

    # Search raw and decoded forms. Steam may HTML-escape or JSON-escape snippets.
    candidates = [listing_html]
    try:
        candidates.append(html_lib.unescape(listing_html))
    except Exception:
        pass
    try:
        candidates.append(listing_html.encode("utf-8", errors="ignore").decode("unicode_escape", errors="ignore"))
    except Exception:
        pass

    patterns = [
        # Classic old listing page.
        r"Market_LoadOrderSpread\s*\(\s*(\d+)\s*\)",
        # Sometimes the histogram URL is embedded directly in scripts or JSON.
        r"itemordershistogram[^\"'<>]*?[?&](?:amp;)?item_nameid=(\d+)",
        r"itemordershistogram[^\"'<>]*?item_nameid%3D(\d+)",
        # Newer/serialized forms if present.
        r"[\"']item_nameid[\"']\s*[:=]\s*[\"']?(\d+)",
        r"[\"']itemNameId[\"']\s*[:=]\s*[\"']?(\d+)",
        r"item_nameid\\?\"?\s*[:=]\s*\\?\"?(\d+)",
        r"ItemActivityTicker\s*\(\s*\d+\s*,\s*(\d+)\s*\)",
    ]

    for text in candidates:
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1)
    return None


def fetch_steam_buy_orders_histogram(
    session: requests.Session,
    item_nameid: str,
    cfg: Config,
    calls: CallCounter,
) -> Optional[dict[str, Any]]:
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": cfg.steam_country,
        "language": cfg.steam_language,
        "currency": STEAM_CURRENCIES[cfg.steam_currency],
        "item_nameid": item_nameid,
        "two_factor": 0,
    }
    headers = steam_headers(cfg)
    headers["Accept"] = "application/json,text/plain,*/*"

    for attempt in range(cfg.steam_max_retries + 1):
        calls.add("steam_itemordershistogram")
        r = session.get(url, params=params, headers=headers, timeout=cfg.request_timeout_seconds)
        if r.status_code == 429:
            if attempt >= cfg.steam_max_retries:
                return {"success": False, "error": "HTTP 429 after retries"}
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else cfg.steam_429_sleep_seconds
            except ValueError:
                wait = cfg.steam_429_sleep_seconds
            print(f"    Steam 429 on order histogram. Waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503, 504):
            if attempt >= cfg.steam_max_retries:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            wait = cfg.steam_429_sleep_seconds
            print(f"    Steam transient {r.status_code}. Waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            return {"success": False, "error": "non-json response"}
        if not data or data.get("success") in (False, 0, "0"):
            return None
        return data
    return None


def strip_steam_market_html_to_text(listing_html: str) -> str:
    """Convert a Steam market listing HTML page into searchable visible text.

    Steam's beta market pages may not expose item_nameid/Market_LoadOrderSpread,
    but the rendered HTML can still contain text like:
      "1,663 requests to buy at NZ$ 1.38 or lower"
    This fallback lets the scanner use that visible buy-order number directly.
    """
    text = listing_html or ""
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    # Keep rough boundaries so prices/quantities do not glue together.
    text = re.sub(r"(?i)<br\s*/?>|</(?:div|p|tr|td|th|li|h[1-6]|button|span)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\u00a0", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def parse_highest_buy_order_from_listing_html(listing_html: str) -> Optional[dict[str, Any]]:
    """Parse highest Steam buy order directly from the beta listing page text.

    This is a fallback for commodity pages where Steam shows the buy-order table
    but hides item_nameid. The headline is the safest target because it states
    the top buy order directly, e.g.
      "1,663 requests to buy at NZ$ 1.38 or lower".
    """
    text = strip_steam_market_html_to_text(listing_html)
    one_line = re.sub(r"\s+", " ", text)

    m = re.search(
        r"([0-9][0-9,]*)\s+requests?\s+to\s+buy\s+at\s+(.{1,40}?)\s+or\s+lower",
        one_line,
        re.I,
    )
    if not m:
        return None

    total_buy_orders = parse_volume(m.group(1))
    price_text = m.group(2).strip()
    highest = money_to_float(price_text)
    if highest is None or highest <= 0:
        return None

    qty_at_top = 0
    # Try to read the first price row after the headline. This is optional; the
    # top price from the headline is what the ROI calculation needs.
    after = one_line[m.end(): m.end() + 300]
    escaped_price = re.escape(price_text)
    q = re.search(escaped_price + r"\s+([0-9][0-9,]*)", after, re.I)
    if q:
        qty_at_top = parse_volume(q.group(1))

    return {
        "success": True,
        "source": "listing_page_visible_buy_order_table",
        "item_nameid": None,
        "highest_buy_order_buyer_price": highest,
        "highest_buy_order_quantity_visible": qty_at_top,
        "total_buy_order_count": total_buy_orders,
        "raw": {
            "headline_price_text": price_text,
            "headline_total_buy_orders": total_buy_orders,
        },
    }


def parse_highest_buy_order(hist: dict[str, Any]) -> tuple[Optional[float], int, int]:
    """Return (highest buy order buyer-pays price, qty at top visible price, total buy orders)."""
    buy_graph = hist.get("buy_order_graph")
    if isinstance(buy_graph, list) and buy_graph:
        first = buy_graph[0]
        if isinstance(first, list) and len(first) >= 2:
            price = money_to_float(first[0])
            qty_at_or_above = parse_volume(first[1])
            total_count = parse_volume(hist.get("buy_order_count"))
            if price is not None and price > 0:
                return price, qty_at_or_above, total_count

    # Fallback: Steam often returns highest_buy_order as cents for two-decimal currencies.
    raw = hist.get("highest_buy_order")
    if raw is not None:
        try:
            cents = int(str(raw))
            price = cents / 100.0
            total_count = parse_volume(hist.get("buy_order_count"))
            return price, 0, total_count
        except ValueError:
            pass

    # Last fallback: parse formatted buy_order_price, e.g. "NZ$ 1.38".
    price = money_to_float(hist.get("buy_order_price"))
    if price is not None and price > 0:
        return price, 0, parse_volume(hist.get("buy_order_count"))
    return None, 0, parse_volume(hist.get("buy_order_count"))


def fetch_steam_highest_buy_order(
    session: requests.Session,
    market_hash_name: str,
    cfg: Config,
    calls: CallCounter,
) -> Optional[dict[str, Any]]:
    try:
        html = fetch_steam_listing_page(session, market_hash_name, cfg, calls)

        # First try the old/histogram route. This gives structured order data when
        # Steam exposes item_nameid in the listing source.
        item_nameid = extract_item_nameid(html)
        if item_nameid:
            hist = fetch_steam_buy_orders_histogram(session, item_nameid, cfg, calls)
            if hist and hist.get("error"):
                # If the histogram endpoint errors, still try the visible table before skipping.
                visible = parse_highest_buy_order_from_listing_html(html)
                return visible if visible else hist
            if hist:
                highest, qty_at_top, total_buy_orders = parse_highest_buy_order(hist)
                if highest is not None:
                    return {
                        "success": True,
                        "source": "itemordershistogram",
                        "item_nameid": item_nameid,
                        "highest_buy_order_buyer_price": highest,
                        "highest_buy_order_quantity_visible": qty_at_top,
                        "total_buy_order_count": total_buy_orders,
                        "raw": hist,
                    }

        # Fallback for current Steam beta/commodity pages: parse the visible text
        # line, e.g. "1,663 requests to buy at NZ$ 1.38 or lower".
        visible = parse_highest_buy_order_from_listing_html(html)
        if visible:
            return visible

        return {"success": False, "error": "could not find item_nameid or visible buy-order table in listing page"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def evaluate_skinport_to_steam_buy_order(
    item: dict[str, Any],
    buy_order: dict[str, Any],
    cfg: Config,
) -> Optional[dict[str, Any]]:
    name = item.get("market_hash_name")
    if not name:
        return None

    skinport_buy_price = float(item["_min_price_float"])  # Skinport currency
    skinport_cost_in_steam_currency = skinport_buy_price * cfg.skinport_to_steam_fx

    steam_buy_order_gross = money_to_float(buy_order.get("highest_buy_order_buyer_price"))
    if steam_buy_order_gross is None or steam_buy_order_gross < cfg.min_highest_buy_order:
        return None

    steam_wallet_net = steam_seller_net_from_buyer_total(steam_buy_order_gross, cfg)
    wallet_profit = steam_wallet_net - skinport_cost_in_steam_currency
    wallet_roi = wallet_profit / skinport_cost_in_steam_currency if skinport_cost_in_steam_currency > 0 else 0.0

    return {
        "market_hash_name": str(name),
        "units_to_buy_on_skinport": 1,
        "skinport_buy_price": round(skinport_buy_price, 2),
        "skinport_currency": cfg.skinport_currency,
        "skinport_to_steam_fx": cfg.skinport_to_steam_fx,
        "skinport_cost_in_steam_currency": round(skinport_cost_in_steam_currency, 2),
        "steam_currency": cfg.steam_currency,
        "steam_sell_price_source": "highest current Steam buy order / buy requests column",
        "steam_item_nameid": buy_order.get("item_nameid"),
        "steam_highest_buy_order_buyer_price": round(float(steam_buy_order_gross), 2),
        "steam_highest_buy_order_quantity_visible": int(buy_order.get("highest_buy_order_quantity_visible") or 0),
        "steam_total_buy_order_count": int(buy_order.get("total_buy_order_count") or 0),
        "estimated_steam_wallet_received_after_fees": round(steam_wallet_net, 2),
        "wallet_profit_after_steam_fees": round(wallet_profit, 2),
        "wallet_roi_after_steam_fees": round(wallet_roi, 4),
        "wallet_roi_after_steam_fees_pct": round(wallet_roi * 100.0, 2),
        "steam_fee_rate": cfg.steam_fee_rate,
        "publisher_fee_rate": cfg.publisher_fee_rate,
        "skinport_quantity_available": int(item.get("quantity") or 0),
        "skinport_suggested_price": money_to_float(item.get("suggested_price")),
        "scmm_candidate_hint": bool(item.get("_scmm_candidate_hint")),
        "skinport_url": item.get("item_page") or item.get("market_page") or "",
        "steam_url": steam_market_link(cfg.app_id, str(name)),
        "note": "One-unit model. Steam wallet received is estimated from instant sale to highest buy order, after Steam/publisher fees.",
    }



def get_skinport_recent_sales_24h(item: dict[str, Any]) -> Optional[int]:
    """Best-effort parser for Skinport recent-sale fields if the API includes them.

    The public /v1/items payload can vary. If no recent-sale field is present,
    return None instead of pretending the item has zero sales.
    """
    keys = [
        "sales_24h",
        "sale_count_24h",
        "sales_count_24h",
        "volume_24h",
        "sold_24h",
        "sales_last_24h",
        "last_24h_sales",
    ]
    for key in keys:
        if key in item and item.get(key) is not None:
            return parse_volume(item.get(key))
    return None


def preselect_cashout_candidates(items: list[dict[str, Any]], cfg: Config, scmm_names: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Choose Skinport rows to test for Steam -> Skinport cashout.

    This is still only a preselection step. The real cashout ranking happens after
    Steam lowest-listing prices are fetched with priceoverview.
    """
    candidates: list[dict[str, Any]] = []
    for item in items:
        name = item.get("market_hash_name")
        min_price = money_to_float(item.get("min_price"))
        qty = int(item.get("quantity") or 0)
        if not name or min_price is None:
            continue
        if qty < cfg.min_skinport_quantity:
            continue
        if not (cfg.min_skinport_price <= min_price <= cfg.max_skinport_price):
            continue

        sales_24h = get_skinport_recent_sales_24h(item)
        if cfg.min_skinport_sales_24h > 0 and sales_24h is not None and sales_24h < cfg.min_skinport_sales_24h:
            continue

        suggested = money_to_float(item.get("suggested_price")) or min_price
        premium_vs_suggested = (min_price - suggested) / suggested if suggested > 0 else 0.0
        enriched = dict(item)
        enriched["_min_price_float"] = float(min_price)
        enriched["_skinport_sales_24h"] = sales_24h
        enriched["_premium_vs_suggested"] = float(premium_vs_suggested)
        enriched["_scmm_candidate_hint"] = str(name).lower() in scmm_lookup
        candidates.append(enriched)

    # For cashout we want items with real Skinport resale value and some depth.
    # This is NOT the final ranking; final ranking uses Steam lowest ask too.
    candidates.sort(
        key=lambda x: (
            1 if x.get("_scmm_candidate_hint") else 0,
            -1 if x.get("_skinport_sales_24h") is None else int(x.get("_skinport_sales_24h") or 0),
            math.log1p(int(x.get("quantity") or 0)),
            float(x.get("_min_price_float") or 0.0),
            float(x.get("_premium_vs_suggested") or 0.0),
        ),
        reverse=True,
    )

    limit = max(0, int(cfg.cashout_candidates))
    if limit == 0 or not candidates:
        return []

    mode = (cfg.candidate_selection or "random").lower().strip()
    rng = random.Random(cfg.random_seed)

    hinted = [x for x in candidates if x.get("_scmm_candidate_hint")]
    hinted_names = {str(x.get("market_hash_name")) for x in hinted}
    non_hinted = [x for x in candidates if str(x.get("market_hash_name")) not in hinted_names]

    if mode == "top":
        return candidates[:limit]

    if mode == "mixed":
        top_count = min(len(candidates), max(1, limit // 2))
        selected = candidates[:top_count]
        selected_names = {str(x.get("market_hash_name")) for x in selected}
        remaining = [x for x in candidates[top_count:] if str(x.get("market_hash_name")) not in selected_names]
        random_count = max(0, limit - len(selected))
        if random_count > 0:
            selected.extend(rng.sample(remaining, k=min(random_count, len(remaining))))
        rng.shuffle(selected)
        return selected[:limit]

    if mode != "random":
        print(f"Unknown candidate selection mode {cfg.candidate_selection!r}; using random.", file=sys.stderr)

    selected = hinted[:limit]
    remaining_slots = max(0, limit - len(selected))
    if remaining_slots > 0:
        selected.extend(rng.sample(non_hinted, k=min(remaining_slots, len(non_hinted))))
    rng.shuffle(selected)
    return selected[:limit]


def fetch_steam_priceoverview(
    session: requests.Session,
    market_hash_name: str,
    cfg: Config,
    calls: CallCounter,
) -> Optional[dict[str, Any]]:
    """Fetch Steam lowest listing / median / volume from priceoverview.

    For the cashout leg you are buying on Steam, so the relevant Steam price is
    the current lowest listing, not the highest buy order.
    """
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": cfg.app_id,
        "currency": STEAM_CURRENCIES[cfg.steam_currency],
        "market_hash_name": market_hash_name,
    }
    headers = steam_headers(cfg)
    headers["Accept"] = "application/json,text/plain,*/*"

    for attempt in range(cfg.steam_max_retries + 1):
        calls.add("steam_priceoverview")
        r = session.get(url, params=params, headers=headers, timeout=cfg.request_timeout_seconds)
        if r.status_code == 429:
            if attempt >= cfg.steam_max_retries:
                return {"success": False, "error": "HTTP 429 after retries"}
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else cfg.steam_429_sleep_seconds
            except ValueError:
                wait = cfg.steam_429_sleep_seconds
            print(f"    Steam 429 on priceoverview. Waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503, 504):
            if attempt >= cfg.steam_max_retries:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            wait = cfg.steam_429_sleep_seconds
            print(f"    Steam transient {r.status_code}. Waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            return {"success": False, "error": "non-json response"}
        if not data or data.get("success") in (False, 0, "0"):
            return None
        return data
    return None


def evaluate_steam_to_skinport_cashout(
    item: dict[str, Any],
    steam_overview: dict[str, Any],
    cfg: Config,
) -> Optional[dict[str, Any]]:
    """Estimate Steam Wallet -> Skinport cash recovery for one skin."""
    name = item.get("market_hash_name")
    if not name:
        return None

    skinport_expected_sale_gross = money_to_float(item.get("min_price"))
    if skinport_expected_sale_gross is None or skinport_expected_sale_gross <= 0:
        return None

    steam_lowest_listing_price = money_to_float(steam_overview.get("lowest_price"))
    if steam_lowest_listing_price is None or steam_lowest_listing_price <= 0:
        return None

    skinport_net_cash = skinport_expected_sale_gross * (1.0 - cfg.skinport_seller_fee_rate)
    skinport_net_cash_in_steam_currency = skinport_net_cash * cfg.skinport_to_steam_fx
    cashout_ratio = skinport_net_cash_in_steam_currency / steam_lowest_listing_price
    cashout_loss = 1.0 - cashout_ratio

    sales_24h = get_skinport_recent_sales_24h(item)

    return {
        "market_hash_name": str(name),
        "units_to_buy_on_steam": 1,
        "steam_currency": cfg.steam_currency,
        "steam_buy_price_source": "Steam current lowest listing / priceoverview lowest_price",
        "steam_lowest_listing_price": round(steam_lowest_listing_price, 2),
        "steam_median_price": money_to_float(steam_overview.get("median_price")),
        "steam_volume_text": steam_overview.get("volume") or "",
        "skinport_currency": cfg.skinport_currency,
        "skinport_expected_sale_gross": round(skinport_expected_sale_gross, 2),
        "skinport_seller_fee_rate": cfg.skinport_seller_fee_rate,
        "skinport_net_cash_after_fee": round(skinport_net_cash, 2),
        "skinport_to_steam_fx": cfg.skinport_to_steam_fx,
        "skinport_net_cash_after_fee_in_steam_currency": round(skinport_net_cash_in_steam_currency, 2),
        "cashout_ratio": round(cashout_ratio, 4),
        "cashout_ratio_pct": round(cashout_ratio * 100.0, 2),
        "cashout_loss_pct": round(cashout_loss * 100.0, 2),
        "skinport_quantity_available": int(item.get("quantity") or 0),
        "skinport_sales_24h_if_available": sales_24h if sales_24h is not None else "unknown",
        "skinport_suggested_price": money_to_float(item.get("suggested_price")),
        "scmm_candidate_hint": bool(item.get("_scmm_candidate_hint")),
        "skinport_url": item.get("item_page") or item.get("market_page") or "",
        "steam_url": steam_market_link(cfg.app_id, str(name)),
        "note": "Cashout estimate: buy one on Steam at lowest listing, wait until tradable, then sell on Skinport at current Skinport min_price minus seller fee.",
    }


def scan_first_leg_skinport_to_steam(
    session: requests.Session,
    items: list[dict[str, Any]],
    cfg: Config,
    calls: CallCounter,
    scmm_names: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    candidates = preselect_skinport_candidates(items, cfg, scmm_names=scmm_names)
    print(f"Skinport rows: {len(items)}; Steam buy-order checks planned: {len(candidates)}")

    rows: list[dict[str, Any]] = []
    for i, item in enumerate(candidates, start=1):
        name = str(item.get("market_hash_name"))
        print(f"[{i}/{len(candidates)}] First leg: checking Steam buy orders: {name}")
        buy_order = fetch_steam_highest_buy_order(session, name, cfg, calls)
        if buy_order and buy_order.get("error"):
            print(f"    skipped: {buy_order['error']}")
        elif buy_order:
            row = evaluate_skinport_to_steam_buy_order(item, buy_order, cfg)
            if row:
                if float(row["wallet_roi_after_steam_fees"]) >= cfg.min_net_roi:
                    rows.append(row)
                    print(
                        f"    KEEP: buy {row['skinport_buy_price']:.2f} {cfg.skinport_currency} "
                        f"-> sell to buy order {row['steam_highest_buy_order_buyer_price']:.2f} {cfg.steam_currency} "
                        f"-> wallet {row['estimated_steam_wallet_received_after_fees']:.2f}; "
                        f"ROI {row['wallet_roi_after_steam_fees_pct']:.2f}%"
                    )
                else:
                    print(
                        f"    skipped: ROI {row['wallet_roi_after_steam_fees_pct']:.2f}% "
                        f"is below required {cfg.min_net_roi * 100:.2f}% after Steam fees"
                    )
        else:
            print("    skipped: no buy-order data")

        delay = cfg.steam_delay_seconds + random.uniform(0, cfg.steam_delay_jitter_seconds)
        time.sleep(delay)

    rows.sort(
        key=lambda r: (
            float(r.get("wallet_roi_after_steam_fees") or 0.0),
            float(r.get("wallet_profit_after_steam_fees") or 0.0),
            int(r.get("steam_highest_buy_order_quantity_visible") or 0),
        ),
        reverse=True,
    )
    return rows


def scan_cashout_steam_to_skinport(
    session: requests.Session,
    items: list[dict[str, Any]],
    cfg: Config,
    calls: CallCounter,
    wallet_limit: Optional[float] = None,
    scmm_names: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    candidates = preselect_cashout_candidates(items, cfg, scmm_names=scmm_names)
    print(f"Cashout Steam lowest-listing checks planned: {len(candidates)}")

    rows: list[dict[str, Any]] = []
    for i, item in enumerate(candidates, start=1):
        name = str(item.get("market_hash_name"))
        print(f"[{i}/{len(candidates)}] Cashout leg: checking Steam lowest listing: {name}")
        overview = fetch_steam_priceoverview(session, name, cfg, calls)
        if overview and overview.get("error"):
            print(f"    skipped: {overview['error']}")
        elif overview:
            row = evaluate_steam_to_skinport_cashout(item, overview, cfg)
            if row:
                steam_price = float(row["steam_lowest_listing_price"])
                ratio = float(row["cashout_ratio"])
                if wallet_limit is not None and wallet_limit > 0 and steam_price > wallet_limit:
                    print(
                        f"    skipped: Steam price {steam_price:.2f} {cfg.steam_currency} "
                        f"is above wallet limit {wallet_limit:.2f} {cfg.steam_currency}"
                    )
                elif ratio < cfg.min_cashout_ratio:
                    print(
                        f"    skipped: cashout ratio {row['cashout_ratio_pct']:.2f}% "
                        f"is below required {cfg.min_cashout_ratio * 100:.2f}%"
                    )
                else:
                    rows.append(row)
                    print(
                        f"    KEEP: spend {row['steam_lowest_listing_price']:.2f} {cfg.steam_currency} "
                        f"-> Skinport net {row['skinport_net_cash_after_fee']:.2f} {cfg.skinport_currency} "
                        f"({row['cashout_ratio_pct']:.2f}% wallet recovery)"
                    )
        else:
            print("    skipped: no Steam priceoverview data")

        delay = cfg.steam_delay_seconds + random.uniform(0, cfg.steam_delay_jitter_seconds)
        time.sleep(delay)

    rows.sort(
        key=lambda r: (
            float(r.get("cashout_ratio") or 0.0),
            float(r.get("skinport_net_cash_after_fee") or 0.0),
        ),
        reverse=True,
    )
    return rows


def choose_cashout_for_wallet(
    cashout_rows: list[dict[str, Any]],
    wallet_balance: float,
    cfg: Config,
) -> Optional[dict[str, Any]]:
    affordable = [r for r in cashout_rows if float(r.get("steam_lowest_listing_price") or 0.0) <= wallet_balance]
    if not affordable:
        return None

    mode = (cfg.cashout_ranking or "biggest-spend").lower().strip()
    if mode == "biggest-spend":
        return max(
            affordable,
            key=lambda r: (
                float(r.get("steam_lowest_listing_price") or 0.0),
                float(r.get("cashout_ratio") or 0.0),
                float(r.get("skinport_net_cash_after_fee") or 0.0),
            ),
        )
    if mode == "best-ratio":
        return max(
            affordable,
            key=lambda r: (
                float(r.get("cashout_ratio") or 0.0),
                float(r.get("skinport_net_cash_after_fee") or 0.0),
            ),
        )
    if mode == "best-total-value":
        return max(
            affordable,
            key=lambda r: (
                float(r.get("skinport_net_cash_after_fee_in_steam_currency") or 0.0)
                + (wallet_balance - float(r.get("steam_lowest_listing_price") or 0.0)),
                float(r.get("cashout_ratio") or 0.0),
            ),
        )

    # Default: extract the largest amount of cash back out through Skinport.
    return max(
        affordable,
        key=lambda r: (
            float(r.get("skinport_net_cash_after_fee") or 0.0),
            float(r.get("cashout_ratio") or 0.0),
        ),
    )


def build_closed_loop_rows(
    first_leg_rows: list[dict[str, Any]],
    cashout_rows: list[dict[str, Any]],
    cfg: Config,
) -> list[dict[str, Any]]:
    loop_rows: list[dict[str, Any]] = []
    for first in first_leg_rows:
        wallet = float(first.get("estimated_steam_wallet_received_after_fees") or 0.0)
        cashout = choose_cashout_for_wallet(cashout_rows, wallet, cfg)
        if not cashout:
            continue

        initial_skinport_cash = float(first.get("skinport_buy_price") or 0.0)
        initial_value_steam = float(first.get("skinport_cost_in_steam_currency") or 0.0)
        steam_spend = float(cashout.get("steam_lowest_listing_price") or 0.0)
        wallet_leftover = wallet - steam_spend
        final_cash_skinport = float(cashout.get("skinport_net_cash_after_fee") or 0.0)
        final_cash_equiv_steam = float(cashout.get("skinport_net_cash_after_fee_in_steam_currency") or 0.0)
        total_value_equiv_steam = final_cash_equiv_steam + wallet_leftover

        cash_only_profit_skinport = final_cash_skinport - initial_skinport_cash
        cash_only_roi = cash_only_profit_skinport / initial_skinport_cash if initial_skinport_cash > 0 else 0.0
        total_value_profit_steam = total_value_equiv_steam - initial_value_steam
        total_value_roi = total_value_profit_steam / initial_value_steam if initial_value_steam > 0 else 0.0

        loop_rows.append({
            "first_leg_skinport_buy": first.get("market_hash_name"),
            "cashout_steam_buy_then_skinport_sell": cashout.get("market_hash_name"),
            "initial_skinport_cash_spent": round(initial_skinport_cash, 2),
            "skinport_currency": cfg.skinport_currency,
            "initial_value_in_steam_currency": round(initial_value_steam, 2),
            "steam_currency": cfg.steam_currency,
            "first_leg_steam_wallet_after_fees": round(wallet, 2),
            "first_leg_wallet_roi_pct": first.get("wallet_roi_after_steam_fees_pct"),
            "cashout_steam_spend": round(steam_spend, 2),
            "steam_wallet_leftover": round(wallet_leftover, 2),
            "cashout_skinport_gross_sale_estimate": cashout.get("skinport_expected_sale_gross"),
            "cashout_skinport_cash_after_fee": round(final_cash_skinport, 2),
            "cashout_ratio_pct": cashout.get("cashout_ratio_pct"),
            "cash_only_profit_skinport_currency": round(cash_only_profit_skinport, 2),
            "cash_only_roi_pct": round(cash_only_roi * 100.0, 2),
            "final_total_value_equiv_steam_currency": round(total_value_equiv_steam, 2),
            "total_value_profit_equiv_steam_currency": round(total_value_profit_steam, 2),
            "total_value_roi_pct": round(total_value_roi * 100.0, 2),
            "first_leg_steam_url": first.get("steam_url"),
            "first_leg_skinport_url": first.get("skinport_url"),
            "cashout_steam_url": cashout.get("steam_url"),
            "cashout_skinport_url": cashout.get("skinport_url"),
            "note": "Closed-loop estimate: Skinport buy -> Steam buy order sale -> Steam lowest listing buy -> Skinport sale after seller fee. One item per leg.",
        })

    loop_rows.sort(
        key=lambda r: (
            float(r.get("cash_only_profit_skinport_currency") or 0.0),
            float(r.get("cash_only_roi_pct") or 0.0),
            float(r.get("total_value_roi_pct") or 0.0),
        ),
        reverse=True,
    )
    return loop_rows


def get_effective_pool_budget(cfg: Config) -> float:
    """Return the total Skinport cash budget used to build the pooled first leg."""
    return cfg.pool_budget_skinport_currency if cfg.pool_budget_skinport_currency > 0 else cfg.budget_skinport_currency


def choose_first_leg_pool(
    first_leg_rows: list[dict[str, Any]],
    cfg: Config,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Choose multiple first-leg Skinport buys to create one pooled Steam wallet.

    This uses 0/1 knapsack in cents:
      - each first-leg row can be selected once
      - total Skinport cash spent must be <= pool budget
      - objective is to maximize total Steam wallet received after Steam fees

    This matches the workflow:
      spend about 100 Skinport cash -> create about 150 Steam wallet -> use that
      pooled Steam wallet to buy one larger cashout skin.
    """
    pool_budget = get_effective_pool_budget(cfg)
    budget_c = to_cents(pool_budget)
    if budget_c <= 0 or not first_leg_rows:
        return [], {}

    # best_wallet[spend_cents] = max Steam wallet cents obtainable with this exact spend.
    best_wallet = [-1] * (budget_c + 1)
    best_profit = [-10**18] * (budget_c + 1)
    parent: list[Optional[tuple[int, int]]] = [None] * (budget_c + 1)
    best_wallet[0] = 0
    best_profit[0] = 0

    usable: list[dict[str, Any]] = []
    for row in first_leg_rows:
        cost = money_to_float(row.get("skinport_buy_price"))
        wallet = money_to_float(row.get("estimated_steam_wallet_received_after_fees"))
        cost_in_steam = money_to_float(row.get("skinport_cost_in_steam_currency"))
        if cost is None or wallet is None or cost <= 0 or wallet <= 0:
            continue
        cost_c = to_cents(cost)
        wallet_c = to_cents(wallet)
        if cost_c <= 0 or cost_c > budget_c:
            continue
        profit_c = wallet_c - to_cents(cost_in_steam if cost_in_steam is not None else cost * cfg.skinport_to_steam_fx)
        enriched = dict(row)
        enriched["_pool_cost_cents"] = cost_c
        enriched["_pool_wallet_cents"] = wallet_c
        enriched["_pool_profit_cents"] = profit_c
        usable.append(enriched)

    for idx, row in enumerate(usable):
        cost_c = int(row["_pool_cost_cents"])
        wallet_c = int(row["_pool_wallet_cents"])
        profit_c = int(row["_pool_profit_cents"])

        # Descending spend prevents using the same row more than once.
        for spend_c in range(budget_c - cost_c, -1, -1):
            if best_wallet[spend_c] < 0:
                continue
            new_spend_c = spend_c + cost_c
            new_wallet_c = best_wallet[spend_c] + wallet_c
            new_profit_c = best_profit[spend_c] + profit_c

            # Maximize Steam wallet; tie-break by profit then by spending more of the budget.
            if (
                new_wallet_c > best_wallet[new_spend_c]
                or (
                    new_wallet_c == best_wallet[new_spend_c]
                    and new_profit_c > best_profit[new_spend_c]
                )
            ):
                best_wallet[new_spend_c] = new_wallet_c
                best_profit[new_spend_c] = new_profit_c
                parent[new_spend_c] = (spend_c, idx)

    possible_spends = [s for s, wallet_c in enumerate(best_wallet) if wallet_c >= 0 and s > 0]
    if not possible_spends:
        return [], {}

    # Choose the spend that produces the biggest Steam wallet pool.
    best_spend_c = max(
        possible_spends,
        key=lambda s: (
            best_wallet[s],
            best_profit[s],
            s,
        ),
    )

    selected_indices: list[int] = []
    cursor = best_spend_c
    while cursor > 0 and parent[cursor] is not None:
        prev_spend, idx = parent[cursor]
        selected_indices.append(idx)
        cursor = prev_spend
    selected_indices.reverse()

    selected_rows = [usable[i] for i in selected_indices]
    for n, row in enumerate(selected_rows, start=1):
        row["pool_buy_order"] = n

    total_cash_spent = sum(float(r.get("skinport_buy_price") or 0.0) for r in selected_rows)
    total_initial_value_steam = sum(float(r.get("skinport_cost_in_steam_currency") or 0.0) for r in selected_rows)
    total_steam_wallet = sum(float(r.get("estimated_steam_wallet_received_after_fees") or 0.0) for r in selected_rows)
    first_leg_wallet_profit = total_steam_wallet - total_initial_value_steam
    first_leg_wallet_roi = first_leg_wallet_profit / total_initial_value_steam if total_initial_value_steam > 0 else 0.0

    summary = {
        "pool_budget_skinport_currency": round(pool_budget, 2),
        "pool_budget_source": "--pool-budget" if cfg.pool_budget_skinport_currency > 0 else "--budget",
        "pool_first_leg_items": len(selected_rows),
        "pool_skinport_cash_spent": round(total_cash_spent, 2),
        "pool_skinport_cash_unspent": round(pool_budget - total_cash_spent, 2),
        "skinport_currency": cfg.skinport_currency,
        "pool_initial_value_in_steam_currency": round(total_initial_value_steam, 2),
        "steam_currency": cfg.steam_currency,
        "pool_steam_wallet_after_fees": round(total_steam_wallet, 2),
        "pool_first_leg_wallet_profit_steam_currency": round(first_leg_wallet_profit, 2),
        "pool_first_leg_wallet_roi_pct": round(first_leg_wallet_roi * 100.0, 2),
        "pool_first_leg_names": " || ".join(str(r.get("market_hash_name")) for r in selected_rows),
    }
    return selected_rows, summary


def rank_pool_cashout_options(
    cashout_rows: list[dict[str, Any]],
    wallet_balance: float,
    pool_summary: dict[str, Any],
    cfg: Config,
) -> list[dict[str, Any]]:
    """Build ranked one-skin cashout options for the pooled Steam wallet."""
    pool_cash_spent = float(pool_summary.get("pool_skinport_cash_spent") or 0.0)
    pool_initial_value_steam = float(pool_summary.get("pool_initial_value_in_steam_currency") or 0.0)

    options: list[dict[str, Any]] = []
    for cashout in cashout_rows:
        steam_spend = float(cashout.get("steam_lowest_listing_price") or 0.0)
        if steam_spend <= 0 or steam_spend > wallet_balance:
            continue
        wallet_leftover = wallet_balance - steam_spend
        wallet_spend_ratio = steam_spend / wallet_balance if wallet_balance > 0 else 0.0
        final_cash_skinport = float(cashout.get("skinport_net_cash_after_fee") or 0.0)
        final_cash_equiv_steam = float(cashout.get("skinport_net_cash_after_fee_in_steam_currency") or 0.0)
        total_value_equiv_steam = final_cash_equiv_steam + wallet_leftover

        cash_only_profit = final_cash_skinport - pool_cash_spent
        cash_only_roi = cash_only_profit / pool_cash_spent if pool_cash_spent > 0 else 0.0
        total_value_profit = total_value_equiv_steam - pool_initial_value_steam
        total_value_roi = total_value_profit / pool_initial_value_steam if pool_initial_value_steam > 0 else 0.0

        option = dict(cashout)
        option.update({
            "_pool_wallet_spend_ratio": wallet_spend_ratio,
            "_pool_wallet_leftover": wallet_leftover,
            "_pool_cash_only_profit": cash_only_profit,
            "_pool_cash_only_roi": cash_only_roi,
            "_pool_total_value_roi": total_value_roi,
            "_pool_total_value_profit": total_value_profit,
            "_pool_total_value_equiv_steam": total_value_equiv_steam,
        })
        options.append(option)

    if not options:
        return []

    # If requested, first try to only consider options that spend a meaningful chunk
    # of the wallet pool. If none exist, fall back to all options.
    min_spend_ratio = cfg.pool_min_wallet_spend_ratio
    if min_spend_ratio > 0:
        filtered = [o for o in options if float(o.get("_pool_wallet_spend_ratio") or 0.0) >= min_spend_ratio]
        if filtered:
            options = filtered

    mode = (cfg.cashout_ranking or "biggest-spend").lower().strip()

    if mode == "best-ratio":
        key_fn = lambda o: (
            float(o.get("cashout_ratio") or 0.0),
            float(o.get("_pool_cash_only_roi") or 0.0),
            float(o.get("steam_lowest_listing_price") or 0.0),
        )
    elif mode == "best-total-value":
        key_fn = lambda o: (
            float(o.get("_pool_total_value_roi") or 0.0),
            float(o.get("_pool_total_value_profit") or 0.0),
            float(o.get("steam_lowest_listing_price") or 0.0),
        )
    elif mode == "best-pool-roi":
        key_fn = lambda o: (
            float(o.get("_pool_cash_only_roi") or 0.0),
            float(o.get("_pool_cash_only_profit") or 0.0),
            float(o.get("steam_lowest_listing_price") or 0.0),
        )
    elif mode == "max-cash":
        key_fn = lambda o: (
            float(o.get("skinport_net_cash_after_fee") or 0.0),
            float(o.get("_pool_cash_only_roi") or 0.0),
            float(o.get("steam_lowest_listing_price") or 0.0),
        )
    else:
        # Default for your requested workflow:
        # "I got 150 wallet, find the biggest skin I can buy to cash out."
        key_fn = lambda o: (
            float(o.get("steam_lowest_listing_price") or 0.0),
            float(o.get("_pool_cash_only_roi") or 0.0),
            float(o.get("cashout_ratio") or 0.0),
        )

    return sorted(options, key=key_fn, reverse=True)


def build_pooled_closed_loop_rows(
    first_leg_rows: list[dict[str, Any]],
    cashout_rows: list[dict[str, Any]],
    cfg: Config,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create pooled first-leg selected rows and one pooled cashout summary row."""
    selected_rows, pool_summary = choose_first_leg_pool(first_leg_rows, cfg)
    if not selected_rows or not pool_summary:
        return [], []

    wallet_balance = float(pool_summary.get("pool_steam_wallet_after_fees") or 0.0)
    ranked_cashouts = rank_pool_cashout_options(cashout_rows, wallet_balance, pool_summary, cfg)
    if not ranked_cashouts:
        return selected_rows, []

    chosen = ranked_cashouts[0]
    steam_spend = float(chosen.get("steam_lowest_listing_price") or 0.0)
    wallet_leftover = float(chosen.get("_pool_wallet_leftover") or 0.0)
    final_cash = float(chosen.get("skinport_net_cash_after_fee") or 0.0)
    cash_profit = float(chosen.get("_pool_cash_only_profit") or 0.0)
    cash_roi = float(chosen.get("_pool_cash_only_roi") or 0.0)
    total_value_profit = float(chosen.get("_pool_total_value_profit") or 0.0)
    total_value_roi = float(chosen.get("_pool_total_value_roi") or 0.0)

    summary = dict(pool_summary)
    summary.update({
        "cashout_ranking_used": cfg.cashout_ranking,
        "pool_min_wallet_spend_ratio": cfg.pool_min_wallet_spend_ratio,
        "cashout_steam_buy_then_skinport_sell": chosen.get("market_hash_name"),
        "cashout_steam_spend": round(steam_spend, 2),
        "pool_steam_wallet_leftover": round(wallet_leftover, 2),
        "pool_wallet_spend_ratio_pct": round(float(chosen.get("_pool_wallet_spend_ratio") or 0.0) * 100.0, 2),
        "cashout_skinport_gross_sale_estimate": chosen.get("skinport_expected_sale_gross"),
        "cashout_skinport_cash_after_fee": round(final_cash, 2),
        "cashout_ratio_pct": chosen.get("cashout_ratio_pct"),
        "cash_only_profit_skinport_currency": round(cash_profit, 2),
        "cash_only_roi_pct": round(cash_roi * 100.0, 2),
        "final_total_value_equiv_steam_currency": round(float(chosen.get("_pool_total_value_equiv_steam") or 0.0), 2),
        "total_value_profit_equiv_steam_currency": round(total_value_profit, 2),
        "total_value_roi_pct": round(total_value_roi * 100.0, 2),
        "cashout_steam_url": chosen.get("steam_url"),
        "cashout_skinport_url": chosen.get("skinport_url"),
        "note": "Pooled closed-loop estimate: select multiple first-leg Skinport buys under total cash budget, sum Steam wallet, then buy one larger Steam cashout skin and estimate Skinport cash after fee.",
    })

    # Add pool totals onto each selected first-leg row for easier reading in CSV.
    enriched_selected: list[dict[str, Any]] = []
    for row in selected_rows:
        out = dict(row)
        out.update({
            "pool_budget_skinport_currency": summary["pool_budget_skinport_currency"],
            "pool_skinport_cash_spent": summary["pool_skinport_cash_spent"],
            "pool_steam_wallet_after_fees": summary["pool_steam_wallet_after_fees"],
            "pool_chosen_cashout_skin": summary["cashout_steam_buy_then_skinport_sell"],
            "pool_cashout_steam_spend": summary["cashout_steam_spend"],
            "pool_cashout_skinport_cash_after_fee": summary["cashout_skinport_cash_after_fee"],
            "pool_cash_only_profit_skinport_currency": summary["cash_only_profit_skinport_currency"],
            "pool_cash_only_roi_pct": summary["cash_only_roi_pct"],
        })
        enriched_selected.append(out)

    return enriched_selected, [summary]


def get_simulated_starting_cash(cfg: Config) -> float:
    """Starting real-cash budget for cashout-only ROI simulation.

    If --starting-skinport-budget is not supplied, fall back to the normal pooled
    budget logic: --pool-budget if set, otherwise --budget.
    """
    return cfg.simulated_initial_skinport_cash if cfg.simulated_initial_skinport_cash > 0 else get_effective_pool_budget(cfg)


def get_simulated_steam_wallet_after_first_leg(cfg: Config) -> float:
    """Convert starting Skinport cash into a simulated Steam wallet pool.

    Example with AUD -> NZD FX 1.10 and 50% first-leg ROI:
      100 AUD starting cash -> 100 * 1.10 * 1.50 = 165 NZD Steam wallet.
    """
    starting_cash = get_simulated_starting_cash(cfg)
    starting_value_steam = starting_cash * cfg.skinport_to_steam_fx
    return starting_value_steam * (1.0 + cfg.assumed_first_leg_roi)


def build_simulated_cashout_from_roi_rows(
    cashout_rows: list[dict[str, Any]],
    cfg: Config,
) -> list[dict[str, Any]]:
    """Rank cashout skins after skipping the first-leg scan.

    This answers:
      "I started with X real cash. Suppose I already turned it into Steam wallet
       with Y% ROI. Which Steam skin can I buy and sell on Skinport, and does the
       final cash beat my original X?"
    """
    starting_cash = get_simulated_starting_cash(cfg)
    starting_value_steam = starting_cash * cfg.skinport_to_steam_fx
    simulated_wallet = get_simulated_steam_wallet_after_first_leg(cfg)
    if starting_cash <= 0 or simulated_wallet <= 0:
        return []

    pool_summary = {
        "pool_skinport_cash_spent": round(starting_cash, 2),
        "pool_initial_value_in_steam_currency": round(starting_value_steam, 2),
        "pool_steam_wallet_after_fees": round(simulated_wallet, 2),
    }
    ranked_cashouts = rank_pool_cashout_options(cashout_rows, simulated_wallet, pool_summary, cfg)

    rows: list[dict[str, Any]] = []
    for rank, chosen in enumerate(ranked_cashouts, start=1):
        steam_spend = float(chosen.get("steam_lowest_listing_price") or 0.0)
        wallet_leftover = float(chosen.get("_pool_wallet_leftover") or 0.0)
        final_cash = float(chosen.get("skinport_net_cash_after_fee") or 0.0)
        cash_profit = float(chosen.get("_pool_cash_only_profit") or 0.0)
        cash_roi = float(chosen.get("_pool_cash_only_roi") or 0.0)
        total_value_profit = float(chosen.get("_pool_total_value_profit") or 0.0)
        total_value_roi = float(chosen.get("_pool_total_value_roi") or 0.0)

        rows.append({
            "scenario_rank": rank,
            "starting_skinport_cash_budget": round(starting_cash, 2),
            "skinport_currency": cfg.skinport_currency,
            "skinport_to_steam_fx": cfg.skinport_to_steam_fx,
            "assumed_first_leg_roi_pct": round(cfg.assumed_first_leg_roi * 100.0, 2),
            "starting_value_in_steam_currency": round(starting_value_steam, 2),
            "simulated_steam_wallet_after_first_leg": round(simulated_wallet, 2),
            "steam_currency": cfg.steam_currency,
            "cashout_steam_buy_then_skinport_sell": chosen.get("market_hash_name"),
            "cashout_steam_spend": round(steam_spend, 2),
            "steam_wallet_leftover": round(wallet_leftover, 2),
            "wallet_spend_ratio_pct": round(float(chosen.get("_pool_wallet_spend_ratio") or 0.0) * 100.0, 2),
            "cashout_skinport_gross_sale_estimate": chosen.get("skinport_expected_sale_gross"),
            "cashout_skinport_cash_after_fee": round(final_cash, 2),
            "cashout_ratio_pct": chosen.get("cashout_ratio_pct"),
            "cash_only_profit_skinport_currency": round(cash_profit, 2),
            "cash_only_roi_pct": round(cash_roi * 100.0, 2),
            "final_total_value_equiv_steam_currency": round(float(chosen.get("_pool_total_value_equiv_steam") or 0.0), 2),
            "total_value_profit_equiv_steam_currency": round(total_value_profit, 2),
            "total_value_roi_pct": round(total_value_roi * 100.0, 2),
            "profitable_cash_only": cash_profit > 0,
            "cashout_steam_url": chosen.get("steam_url"),
            "cashout_skinport_url": chosen.get("skinport_url"),
            "note": "Cashout-only simulation: skip first-leg scan, assume starting cash became Steam wallet by the chosen ROI, then test one Steam->Skinport cashout skin.",
        })

    return rows


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> None:
    p = Path(path)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    for row in rows[1:]:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_table(rows: list[dict[str, Any]], title: str, columns: list[str], limit: int = 15) -> None:
    print("\n" + title)
    print("=" * len(title))
    if not rows:
        print("No rows.")
        return
    rows = rows[:limit]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print(" | ".join(c.ljust(widths[c]) for c in columns))
    print("-+-".join("-" * widths[c] for c in columns))
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def parse_args(argv: Optional[list[str]] = None) -> Config:
    p = argparse.ArgumentParser(
        description="Scan ONE-ITEM Skinport -> Steam Wallet token opportunities using Steam highest buy orders."
    )
    p.add_argument("--app-id", type=int, default=RUST_APP_ID, help="Steam app id. Rust = 252490.")
    p.add_argument("--skinport-currency", default="AUD", help="Skinport currency. NZD is not supported by Skinport public API.")
    p.add_argument("--steam-currency", default="NZD", help="Steam currency, e.g. NZD, AUD, USD.")
    p.add_argument("--skinport-to-steam-fx", type=float, default=1.10, help="1 Skinport currency unit = this many Steam currency units.")
    p.add_argument("--budget", type=float, default=500.0, help="Maximum Skinport buy price for a single item, in Skinport currency. Also used as the total pool budget when --pool-budget is 0.")
    p.add_argument("--pool-budget", type=float, default=0.0, help="Total Skinport cash budget for the pooled closed-loop. Example: 100. 0 = use --budget.")
    p.add_argument("--starting-skinport-budget", type=float, default=0.0, help="For cashout-only simulation: original Skinport cash budget to test profit against. Example: 100. 0 = use --pool-budget or --budget.")
    p.add_argument("--assume-first-leg-roi", type=float, default=0.0, help="For cashout-only simulation: assumed Skinport -> Steam wallet ROI after fees. Example: 0.50 means +50%% wallet ROI.")
    p.add_argument("--mode", choices=["first-leg", "cashout", "closed-loop"], default="closed-loop", help="first-leg = Skinport -> Steam only; cashout = Steam -> Skinport only; closed-loop = run both and pair them.")
    p.add_argument("--max-candidates", type=int, default=120, help="How many Skinport candidates to check on Steam.")
    p.add_argument("--min-net-roi", type=float, default=0.20, help="Minimum net ROI after Steam fees to keep. Example: 0.20 keeps only 20%%+ ROI rows.")
    p.add_argument("--steam-wallet-balance", type=float, default=0.0, help="For cashout mode, only keep Steam -> Skinport rows affordable within this Steam wallet balance. 0 = no wallet limit.")
    p.add_argument("--cashout-candidates", type=int, default=120, help="How many Steam -> Skinport cashout candidates to check on Steam priceoverview.")
    p.add_argument("--skinport-seller-fee-rate", type=float, default=0.08, help="Skinport seller fee used for cashout estimate. Example: 0.08 = 8%%.")
    p.add_argument("--min-cashout-ratio", type=float, default=0.0, help="Minimum Steam-wallet recovery ratio for cashout rows. Example: 0.85 keeps 85%%+ recovery.")
    p.add_argument("--min-skinport-sales-24h", type=int, default=0, help="If Skinport supplies a recent-sales field, require at least this many 24h sales for cashout candidates.")
    p.add_argument("--cashout-ranking", choices=["biggest-spend", "max-cash", "best-ratio", "best-total-value", "best-pool-roi"], default="biggest-spend", help="How closed-loop chooses the cashout item. biggest-spend tries to use as much of the Steam wallet pool as possible.")
    p.add_argument("--pool-min-wallet-spend-ratio", type=float, default=0.0, help="For pooled closed-loop, only consider cashout skins spending at least this fraction of the pooled Steam wallet if possible. Example: 0.90.")
    p.add_argument("--candidate-selection", choices=["random", "top", "mixed"], default="top", help="Which Skinport skins to check: random gives different skins each run; top is old behavior; mixed is half top, half random.")
    p.add_argument("--random-seed", type=int, default=None, help="Optional seed for repeatable random candidate choices. Omit for different skins each run.")
    p.add_argument("--use-scmm-candidates", action="store_true", help="Use SCMM market-deal/API names as candidate hints before calling Steam. Cached by --scmm-cache-seconds.")
    p.add_argument("--scmm-api-url", default="", help="Optional exact SCMM JSON endpoint copied from browser Network tab. If blank, common endpoints plus the market-deals page are tried.")
    p.add_argument("--scmm-deals-url", default="https://rust.scmm.app/market-deals", help="SCMM market deals page used as an HTML fallback.")
    p.add_argument("--scmm-candidate-limit", type=int, default=120, help="Maximum number of SCMM candidate names to keep as hints.")
    p.add_argument("--scmm-cache-seconds", type=int, default=600, help="How long to cache SCMM candidate names. 0 disables cache.")
    p.add_argument("--scmm-cache-file", default="scmm_candidates_cache.json", help="Local cache file for SCMM candidate names.")
    p.add_argument("--min-skinport-price", type=float, default=0.50)
    p.add_argument("--max-skinport-price", type=float, default=500.00)
    p.add_argument("--min-skinport-quantity", type=int, default=1)
    p.add_argument("--steam-delay", type=float, default=8.0, help="Seconds to wait between Steam item checks.")
    p.add_argument("--steam-jitter", type=float, default=2.0, help="Random extra seconds between Steam item checks.")
    p.add_argument("--steam-country", default="NZ")
    p.add_argument("--steam-language", default="english")
    p.add_argument("--output", default="skinport_to_steam_buy_order_tokens.csv")
    p.add_argument("--cashout-output", default="steam_to_skinport_cashout.csv")
    p.add_argument("--closed-loop-output", default="closed_loop_skinport_steam_skinport.csv")
    p.add_argument("--pool-selected-output", default="pooled_first_leg_selected.csv")
    p.add_argument("--pool-summary-output", default="pooled_closed_loop_summary.csv")
    p.add_argument("--simulated-cashout-output", default="simulated_cashout_from_roi.csv")
    p.add_argument("--call-log", default="call_log_buy_order_only.json")
    p.add_argument("--debug-dir", default="steam_debug_pages", help="Where to save Steam HTML pages when item_nameid cannot be found.")
    p.add_argument("--steam-cookie", default="", help="Optional raw Cookie header copied from your browser for steamcommunity.com. Useful if Steam serves beta pages without item_nameid.")
    p.add_argument("--steam-cookie-file", default="", help="Optional text file containing the raw Steam Cookie header.")
    p.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    args = p.parse_args(argv)

    skinport_currency = args.skinport_currency.upper()
    steam_currency = args.steam_currency.upper()
    if skinport_currency not in SKINPORT_CURRENCIES:
        supported = ", ".join(sorted(SKINPORT_CURRENCIES))
        raise SystemExit(
            f"Unsupported --skinport-currency {skinport_currency}. Skinport public API does not support NZD. "
            f"Use one of: {supported}. For NZ workflow use --skinport-currency AUD --steam-currency NZD --skinport-to-steam-fx <AUDNZD>."
        )
    if steam_currency not in STEAM_CURRENCIES:
        raise SystemExit(f"Unsupported --steam-currency {steam_currency}.")
    if args.skinport_to_steam_fx <= 0:
        raise SystemExit("--skinport-to-steam-fx must be positive.")
    if args.min_net_roi < 0:
        raise SystemExit("--min-net-roi must be 0 or positive. Example: 0.20 means 20%.")
    if args.pool_budget < 0:
        raise SystemExit("--pool-budget must be 0 or positive.")
    if args.starting_skinport_budget < 0:
        raise SystemExit("--starting-skinport-budget must be 0 or positive.")
    if args.assume_first_leg_roi < 0:
        raise SystemExit("--assume-first-leg-roi must be 0 or positive. Example: 0.50 means +50%.")
    if args.steam_wallet_balance < 0:
        raise SystemExit("--steam-wallet-balance must be 0 or positive.")
    if args.cashout_candidates < 0:
        raise SystemExit("--cashout-candidates must be 0 or positive.")
    if not (0 <= args.skinport_seller_fee_rate < 1):
        raise SystemExit("--skinport-seller-fee-rate must be between 0 and 1. Example: 0.08 means 8%.")
    if args.min_cashout_ratio < 0:
        raise SystemExit("--min-cashout-ratio must be 0 or positive. Example: 0.85 means 85%.")
    if args.min_skinport_sales_24h < 0:
        raise SystemExit("--min-skinport-sales-24h must be 0 or positive.")
    if not (0 <= args.pool_min_wallet_spend_ratio <= 1):
        raise SystemExit("--pool-min-wallet-spend-ratio must be between 0 and 1. Example: 0.90.")
    if args.scmm_candidate_limit < 0:
        raise SystemExit("--scmm-candidate-limit must be 0 or positive.")
    if args.scmm_cache_seconds < 0:
        raise SystemExit("--scmm-cache-seconds must be 0 or positive.")
    candidate_selection = args.candidate_selection.lower().strip()

    steam_cookie = args.steam_cookie.strip()
    if args.steam_cookie_file:
        steam_cookie = Path(args.steam_cookie_file).read_text(encoding="utf-8").strip()

    return Config(
        app_id=args.app_id,
        skinport_currency=skinport_currency,
        steam_currency=steam_currency,
        skinport_to_steam_fx=args.skinport_to_steam_fx,
        budget_skinport_currency=args.budget,
        pool_budget_skinport_currency=args.pool_budget,
        simulated_initial_skinport_cash=args.starting_skinport_budget,
        assumed_first_leg_roi=args.assume_first_leg_roi,
        mode=args.mode,
        max_candidates=args.max_candidates,
        min_net_roi=args.min_net_roi,
        steam_wallet_balance=args.steam_wallet_balance,
        cashout_candidates=args.cashout_candidates,
        skinport_seller_fee_rate=args.skinport_seller_fee_rate,
        min_cashout_ratio=args.min_cashout_ratio,
        min_skinport_sales_24h=args.min_skinport_sales_24h,
        cashout_ranking=args.cashout_ranking,
        pool_min_wallet_spend_ratio=args.pool_min_wallet_spend_ratio,
        candidate_selection=candidate_selection,
        random_seed=args.random_seed,
        use_scmm_candidates=args.use_scmm_candidates,
        scmm_deals_url=args.scmm_deals_url,
        scmm_api_url=args.scmm_api_url,
        scmm_candidate_limit=args.scmm_candidate_limit,
        scmm_cache_seconds=args.scmm_cache_seconds,
        scmm_cache_file=args.scmm_cache_file,
        min_skinport_quantity=args.min_skinport_quantity,
        min_skinport_price=args.min_skinport_price,
        max_skinport_price=args.max_skinport_price,
        steam_delay_seconds=args.steam_delay,
        steam_delay_jitter_seconds=args.steam_jitter,
        steam_country=args.steam_country.upper(),
        steam_language=args.steam_language,
        output_csv=args.output,
        cashout_output_csv=args.cashout_output,
        closed_loop_output_csv=args.closed_loop_output,
        pool_selected_output_csv=args.pool_selected_output,
        pool_summary_output_csv=args.pool_summary_output,
        simulated_cashout_output_csv=args.simulated_cashout_output,
        call_log_json=args.call_log,
        debug_dir=args.debug_dir,
        steam_cookie=steam_cookie,
        user_agent=args.user_agent,
    )



def main(argv: Optional[list[str]] = None) -> int:
    cfg = parse_args(argv)
    calls = CallCounter()

    print("Skinport / Steam scanner")
    print(f"Mode: {cfg.mode}")
    print(f"App ID: {cfg.app_id}")
    print(f"Skinport currency: {cfg.skinport_currency}")
    print(f"Steam currency: {cfg.steam_currency}")
    print(f"FX: 1 {cfg.skinport_currency} = {cfg.skinport_to_steam_fx:.4f} {cfg.steam_currency}")
    print(f"Candidate selection: {cfg.candidate_selection}" + (f" (seed={cfg.random_seed})" if cfg.random_seed is not None else ""))
    print(f"SCMM candidate hints: {'ON' if cfg.use_scmm_candidates else 'OFF'}" + (f" (limit={cfg.scmm_candidate_limit}, cache={cfg.scmm_cache_seconds}s)" if cfg.use_scmm_candidates else ""))
    print(f"Steam delay: {cfg.steam_delay_seconds}s + jitter up to {cfg.steam_delay_jitter_seconds}s")
    print(f"Skinport seller fee for cashout leg: {cfg.skinport_seller_fee_rate * 100:.2f}%")
    print("NOTE: First leg sells to current highest Steam buy order. Cashout leg buys from current Steam lowest listing.")
    if cfg.mode in ("first-leg", "closed-loop"):
        print(f"First-leg max candidates: {cfg.max_candidates}")
        print(f"First-leg minimum net ROI after Steam fees: {cfg.min_net_roi * 100:.2f}%")
    if cfg.mode in ("cashout", "closed-loop"):
        print(f"Cashout candidates: {cfg.cashout_candidates}")
        print(f"Minimum cashout wallet recovery ratio: {cfg.min_cashout_ratio * 100:.2f}%")
        if cfg.mode == "closed-loop":
            print(f"Pooled first-leg budget: {get_effective_pool_budget(cfg):.2f} {cfg.skinport_currency}")
            print(f"Pooled cashout minimum wallet spend ratio: {cfg.pool_min_wallet_spend_ratio * 100:.2f}%")
        if cfg.mode == "cashout" and (cfg.simulated_initial_skinport_cash > 0 or cfg.assumed_first_leg_roi > 0):
            print(
                f"Cashout-only simulation: start with {get_simulated_starting_cash(cfg):.2f} {cfg.skinport_currency}, "
                f"assume first-leg ROI {cfg.assumed_first_leg_roi * 100:.2f}%, "
                f"simulated Steam wallet {get_simulated_steam_wallet_after_first_leg(cfg):.2f} {cfg.steam_currency}"
            )
        elif cfg.steam_wallet_balance > 0:
            print(f"Steam wallet balance limit: {cfg.steam_wallet_balance:.2f} {cfg.steam_currency}")
        print(f"Closed-loop cashout choice ranking: {cfg.cashout_ranking}")
    if cfg.steam_cookie:
        print("Using Steam browser cookie for Steam requests.")
    print(f"Debug HTML dir for failed item_nameid pages: {cfg.debug_dir}\n")

    first_leg_rows: list[dict[str, Any]] = []
    cashout_rows: list[dict[str, Any]] = []
    closed_loop_rows: list[dict[str, Any]] = []
    simulated_cashout_rows: list[dict[str, Any]] = []

    with requests.Session() as session:
        print("Fetching Skinport tradable items...")
        items = fetch_skinport_items(session, cfg, calls)
        print(f"Fetched Skinport rows: {len(items)}")

        scmm_names: set[str] = set()
        if cfg.use_scmm_candidates:
            scmm_names = fetch_scmm_candidate_names(session, cfg, calls)
            skinport_name_set = {str(item.get("market_hash_name")).lower() for item in items if item.get("market_hash_name")}
            matched = sum(1 for name in scmm_names if str(name).lower() in skinport_name_set)
            print(f"SCMM names matching Skinport item names: {matched}/{len(scmm_names)}")

        if cfg.mode in ("first-leg", "closed-loop"):
            first_leg_rows = scan_first_leg_skinport_to_steam(session, items, cfg, calls, scmm_names=scmm_names)

        if cfg.mode in ("cashout", "closed-loop"):
            wallet_limit: Optional[float] = None
            if cfg.mode == "cashout" and (cfg.simulated_initial_skinport_cash > 0 or cfg.assumed_first_leg_roi > 0):
                wallet_limit = get_simulated_steam_wallet_after_first_leg(cfg)
                print(
                    f"\nCashout scan wallet limit from simulated first-leg result: "
                    f"{wallet_limit:.2f} {cfg.steam_currency}"
                )
            elif cfg.mode == "cashout" and cfg.steam_wallet_balance > 0:
                wallet_limit = cfg.steam_wallet_balance
            elif cfg.mode == "closed-loop" and first_leg_rows:
                _pool_selected_preview, _pool_summary_preview = choose_first_leg_pool(first_leg_rows, cfg)
                if _pool_summary_preview:
                    wallet_limit = float(_pool_summary_preview.get("pool_steam_wallet_after_fees") or 0.0)
                    print(f"\nCashout scan wallet limit from pooled first-leg wallet: {wallet_limit:.2f} {cfg.steam_currency}")
                    print(
                        f"Pooled first leg preview: spend {_pool_summary_preview.get('pool_skinport_cash_spent')} "
                        f"{cfg.skinport_currency} -> wallet {_pool_summary_preview.get('pool_steam_wallet_after_fees')} "
                        f"{cfg.steam_currency} using {_pool_summary_preview.get('pool_first_leg_items')} items"
                    )
                else:
                    wallet_limit = max(float(r.get("estimated_steam_wallet_received_after_fees") or 0.0) for r in first_leg_rows)
                    print(f"\nCashout scan wallet limit from best first-leg wallet: {wallet_limit:.2f} {cfg.steam_currency}")
            cashout_rows = scan_cashout_steam_to_skinport(session, items, cfg, calls, wallet_limit=wallet_limit, scmm_names=scmm_names)

    if first_leg_rows:
        write_csv(first_leg_rows, cfg.output_csv)
        print_table(
            first_leg_rows,
            f"Best first-leg Skinport -> Steam Wallet outputs, {cfg.min_net_roi * 100:.0f}%+ net after Steam fees",
            [
                "market_hash_name",
                "skinport_buy_price",
                "skinport_cost_in_steam_currency",
                "steam_highest_buy_order_buyer_price",
                "estimated_steam_wallet_received_after_fees",
                "wallet_profit_after_steam_fees",
                "wallet_roi_after_steam_fees_pct",
                "steam_highest_buy_order_quantity_visible",
                "scmm_candidate_hint",
            ],
            limit=15,
        )
        print(f"\nKept {len(first_leg_rows)} first-leg skins with net ROI >= {cfg.min_net_roi * 100:.2f}% after Steam fees.")
        print(f"Wrote: {cfg.output_csv}")
    elif cfg.mode in ("first-leg", "closed-loop"):
        write_csv([], cfg.output_csv)
        print(f"\nNo first-leg rows passed the {cfg.min_net_roi * 100:.2f}% net ROI filter.")
        print(f"Wrote empty file: {cfg.output_csv}")

    if cashout_rows:
        write_csv(cashout_rows, cfg.cashout_output_csv)
        print_table(
            cashout_rows,
            "Best Steam Wallet -> Skinport cashout candidates",
            [
                "market_hash_name",
                "steam_lowest_listing_price",
                "skinport_expected_sale_gross",
                "skinport_net_cash_after_fee",
                "skinport_net_cash_after_fee_in_steam_currency",
                "cashout_ratio_pct",
                "skinport_quantity_available",
                "skinport_sales_24h_if_available",
                "scmm_candidate_hint",
            ],
            limit=15,
        )
        print(f"\nKept {len(cashout_rows)} cashout candidates.")
        print(f"Wrote: {cfg.cashout_output_csv}")
    elif cfg.mode in ("cashout", "closed-loop"):
        write_csv([], cfg.cashout_output_csv)
        print("\nNo Steam -> Skinport cashout rows found.")
        print(f"Wrote empty file: {cfg.cashout_output_csv}")

    if cfg.mode == "cashout" and (cfg.simulated_initial_skinport_cash > 0 or cfg.assumed_first_leg_roi > 0):
        simulated_cashout_rows = build_simulated_cashout_from_roi_rows(cashout_rows, cfg) if cashout_rows else []
        write_csv(simulated_cashout_rows, cfg.simulated_cashout_output_csv)
        if simulated_cashout_rows:
            print_table(
                simulated_cashout_rows,
                "Cashout-only profit test: starting cash + assumed first-leg ROI -> Steam wallet -> Skinport cash",
                [
                    "starting_skinport_cash_budget",
                    "assumed_first_leg_roi_pct",
                    "simulated_steam_wallet_after_first_leg",
                    "cashout_steam_buy_then_skinport_sell",
                    "cashout_steam_spend",
                    "steam_wallet_leftover",
                    "cashout_skinport_cash_after_fee",
                    "cash_only_profit_skinport_currency",
                    "cash_only_roi_pct",
                    "cashout_ratio_pct",
                    "wallet_spend_ratio_pct",
                    "profitable_cash_only",
                ],
                limit=15,
            )
            best = simulated_cashout_rows[0]
            print(
                f"\nBest cash-only result: {best.get('cash_only_profit_skinport_currency')} "
                f"{cfg.skinport_currency} profit, ROI {best.get('cash_only_roi_pct')}%, "
                f"profitable={best.get('profitable_cash_only')}"
            )
        else:
            print("\nNo simulated cashout profit rows could be built from the scanned cashout candidates.")
        print(f"Wrote: {cfg.simulated_cashout_output_csv}")

    if cfg.mode == "closed-loop" and first_leg_rows and cashout_rows:
        closed_loop_rows = build_closed_loop_rows(first_leg_rows, cashout_rows, cfg)
        write_csv(closed_loop_rows, cfg.closed_loop_output_csv)
        print_table(
            closed_loop_rows,
            "Best old one-item closed-loop Skinport -> Steam -> Skinport cash outcomes",
            [
                "first_leg_skinport_buy",
                "cashout_steam_buy_then_skinport_sell",
                "initial_skinport_cash_spent",
                "first_leg_steam_wallet_after_fees",
                "cashout_steam_spend",
                "steam_wallet_leftover",
                "cashout_skinport_cash_after_fee",
                "cash_only_profit_skinport_currency",
                "cash_only_roi_pct",
                "total_value_roi_pct",
            ],
            limit=15,
        )
        print(f"\nWrote: {cfg.closed_loop_output_csv}")

        pooled_selected_rows, pooled_summary_rows = build_pooled_closed_loop_rows(first_leg_rows, cashout_rows, cfg)
        write_csv(pooled_selected_rows, cfg.pool_selected_output_csv)
        write_csv(pooled_summary_rows, cfg.pool_summary_output_csv)

        if pooled_summary_rows:
            print_table(
                pooled_summary_rows,
                "Best POOLED closed-loop cashout: total Skinport cash -> total Steam wallet pool -> one larger Skinport cashout",
                [
                    "pool_budget_skinport_currency",
                    "pool_skinport_cash_spent",
                    "pool_first_leg_items",
                    "pool_steam_wallet_after_fees",
                    "cashout_steam_buy_then_skinport_sell",
                    "cashout_steam_spend",
                    "pool_steam_wallet_leftover",
                    "cashout_skinport_cash_after_fee",
                    "cash_only_profit_skinport_currency",
                    "cash_only_roi_pct",
                    "total_value_roi_pct",
                    "pool_wallet_spend_ratio_pct",
                ],
                limit=5,
            )
        else:
            print("\nNo pooled closed-loop cashout could be built from the scanned rows.")

        print(f"\nWrote: {cfg.pool_selected_output_csv}")
        print(f"Wrote: {cfg.pool_summary_output_csv}")
    elif cfg.mode == "closed-loop":
        write_csv([], cfg.closed_loop_output_csv)
        write_csv([], cfg.pool_selected_output_csv)
        write_csv([], cfg.pool_summary_output_csv)
        print(f"Wrote empty file: {cfg.closed_loop_output_csv}")
        print(f"Wrote empty file: {cfg.pool_selected_output_csv}")
        print(f"Wrote empty file: {cfg.pool_summary_output_csv}")

    Path(cfg.call_log_json).write_text(json.dumps(calls.as_dict(), indent=2), encoding="utf-8")
    print(f"Wrote: {cfg.call_log_json}")
    print("Reminder: verify Steam/Skinport confirmations because fee rounding, listing depth, trade cooldowns, and actual sale prices can differ.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
