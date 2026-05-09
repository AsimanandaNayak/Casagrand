from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

try:
    from rapidfuzz import fuzz as rf_fuzz
    from rapidfuzz import process as rf_process
except Exception:  # pragma: no cover
    rf_fuzz = None
    rf_process = None


APP_DIR = Path(__file__).resolve().parent
DATA_PATH = APP_DIR / "data.json"

DEFAULT_FUZZY_CUTOFF = 0.78
MAX_CONTAINS_KEYS = 25
DEFAULT_BHK_FUZZY_CUTOFF = 0.72


def _toon_escape_cell(value: str) -> str:
    """
    Minimal CSV-style escaping for TOON table cells.
    Quote if the value contains commas, quotes, newlines, or leading/trailing spaces.
    """
    needs_quotes = (
        "," in value
        or "\n" in value
        or "\r" in value
        or '"' in value
        or (value[:1].isspace() if value else False)
        or (value[-1:].isspace() if value else False)
    )
    if not needs_quotes:
        return value
    return '"' + value.replace('"', '""') + '"'


def _toon_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # JSON-style numbers
        return str(value)
    if isinstance(value, str):
        return _toon_escape_cell(value)
    # Fallback: represent unknown types as JSON string
    return _toon_escape_cell(json.dumps(value, ensure_ascii=False))


def encode_toon(data: Any) -> str:
    """
    Encode Python data (JSON data model) to a TOON-like text representation.
    Optimized for:
      - dicts (YAML-like indentation)
      - uniform arrays of objects (tabular arrays)
      - arrays of primitives (single-line)
    """

    def _encode(v: Any, indent: int) -> list[str]:
        pad = "  " * indent

        if isinstance(v, dict):
            lines: list[str] = []
            for k, vv in v.items():
                if isinstance(vv, dict):
                    lines.append(f"{pad}{k}:")
                    lines.extend(_encode(vv, indent + 1))
                elif isinstance(vv, list):
                    # Arrays: primitives -> one line, objects -> tabular when possible
                    if all(not isinstance(x, (dict, list)) for x in vv):
                        cells = ",".join(_toon_scalar(x) for x in vv)
                        lines.append(f"{pad}{k}[{len(vv)}]: {cells}")
                    elif vv and all(isinstance(x, dict) for x in vv):
                        # Make a stable union of fields across rows
                        fields: list[str] = []
                        seen: set[str] = set()
                        for row in vv:
                            for fk in row.keys():
                                if fk not in seen:
                                    seen.add(fk)
                                    fields.append(str(fk))
                        lines.append(f"{pad}{k}[{len(vv)}]{{{','.join(fields)}}}:")
                        for row in vv:
                            row_cells = ",".join(_toon_scalar(row.get(fk)) for fk in fields)
                            lines.append(f"{pad}  {row_cells}")
                    else:
                        # Non-uniform / nested arrays: fall back to JSON for safety
                        lines.append(f"{pad}{k}: {_toon_scalar(vv)}")
                else:
                    lines.append(f"{pad}{k}: {_toon_scalar(vv)}")
            return lines

        if isinstance(v, list):
            # Top-level list: encode as JSON fallback (not expected for this API response)
            return [pad + _toon_scalar(v)]

        return [pad + _toon_scalar(v)]

    return "\n".join(_encode(data, 0)).rstrip() + "\n"


def _normalize_location(value: Any) -> str:
    """
    Normalize location strings to improve match quality:
    - cast to string, lower-case
    - trim and collapse whitespace
    - normalize " , " spacing around commas
    - remove most punctuation (keep alphanumerics/spaces)
    """
    if value is None:
        return ""
    s = str(value).strip().lower()
    # Normalize commas/spaces first (common in the dataset)
    s = re.sub(r"\s*,\s*", ",", s)
    # Turn punctuation (including commas) into spaces for tolerant matching
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_bhk(value: Any) -> str:
    """
    Normalize BHK strings for matching:
    - lower-case
    - remove spaces
    - normalize common separators
    Examples:
      "3 BHK" -> "3bhk"
      "3BHK + 2T" -> "3bhk+2t"
    """
    if value is None:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"\s+", "", s)
    s = s.replace("bhk+", "bhk+").replace("+", "+")
    return s


def _load_data() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        raise RuntimeError(f"Missing data file: {DATA_PATH}")
    with DATA_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise RuntimeError("data.json must be a JSON array of objects")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.append(item)
    return rows


app = FastAPI(title="Location Data API", version="1.0.0")

DATA: list[dict[str, Any]] = []
LOCATION_INDEX: dict[str, list[dict[str, Any]]] = {}
LOCATION_CANONICAL: dict[str, str] = {}
LOCATION_KEYS: list[str] = []

def _similarity(a: str, b: str) -> float:
    """
    Returns a similarity score 0..1.
    Prefers RapidFuzz if available, otherwise falls back to difflib.
    """
    if rf_fuzz is not None:
        return float(rf_fuzz.WRatio(a, b)) / 100.0
    return float(difflib.SequenceMatcher(a=a, b=b).ratio())


def _compact_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Remove null/empty fields from a property row to reduce response size.
    Keeps 0/False values, removes:
    - None
    - "" (after stripping)
    """
    out: dict[str, Any] = {}
    for k, v in item.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out


def _best_fuzzy_key(query_key: str, *, cutoff: float) -> tuple[str | None, float]:
    """
    Returns (best_key, score) where score is 0..1.
    Uses normalized keys to support typo tolerance.
    """
    if not query_key or not LOCATION_KEYS:
        return None, 0.0

    cutoff = max(0.0, min(1.0, cutoff))

    if rf_process is not None and rf_fuzz is not None:
        hit = rf_process.extractOne(
            query_key,
            LOCATION_KEYS,
            scorer=rf_fuzz.WRatio,
            score_cutoff=cutoff * 100.0,
        )
        if not hit:
            return None, 0.0
        best, score_0_100, _idx = hit
        return str(best), float(score_0_100) / 100.0

    candidates = difflib.get_close_matches(query_key, LOCATION_KEYS, n=1, cutoff=cutoff)
    if not candidates:
        return None, 0.0
    best = candidates[0]
    return best, _similarity(query_key, best)

def _top_fuzzy_keys(query_key: str, *, n: int = 5) -> list[dict[str, Any]]:
    """
    Returns top N candidate keys with their similarity scores (0..1).
    Useful for suggestions when we can't confidently auto-match.
    """
    if not query_key or not LOCATION_KEYS or n <= 0:
        return []

    if rf_process is not None and rf_fuzz is not None:
        hits = rf_process.extract(
            query_key,
            LOCATION_KEYS,
            scorer=rf_fuzz.WRatio,
            limit=n,
        )
        out: list[dict[str, Any]] = []
        for key, score_0_100, _idx in hits:
            k = str(key)
            out.append({"location": LOCATION_CANONICAL.get(k, k), "score": float(score_0_100) / 100.0})
        out.sort(key=lambda d: d["score"], reverse=True)
        return out

    # difflib fallback
    candidates = difflib.get_close_matches(query_key, LOCATION_KEYS, n=n, cutoff=0.0)
    out: list[dict[str, Any]] = []
    for k in candidates:
        out.append({"location": LOCATION_CANONICAL.get(k, k), "score": _similarity(query_key, k)})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out

def _contains_keys(query_key: str) -> list[str]:
    """
    Returns location keys that contain the query.
    Uses simple normalized substring checks, plus a word-boundary-ish regex
    to reduce false positives (e.g. 'ram' matching everything).
    """
    if not query_key or not LOCATION_KEYS:
        return []

    # Prefer whole-token matches when query is short.
    # "Tambaram" should match "Tambaram, Chennai".
    token_pat = None
    if len(query_key) >= 4:
        token_pat = re.compile(rf"(^|[^a-z0-9]){re.escape(query_key)}([^a-z0-9]|$)")

    matches: list[str] = []
    for key in LOCATION_KEYS:
        if query_key in key:
            if token_pat is None or token_pat.search(key):
                matches.append(key)
    return matches


def _match_bhk(rows: list[dict[str, Any]], bhk_input: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Returns (filtered_rows, meta) where meta describes the bhk match.
    Matching strategy: exact -> contains/token -> fuzzy.
    """
    q = _normalize_bhk(bhk_input)
    if not q:
        return rows, None

    # Build candidate map: normalized_bhk -> (canonical, rows)
    by_key: dict[str, list[dict[str, Any]]] = {}
    canonical: dict[str, str] = {}
    for r in rows:
        raw = r.get("BHK")
        k = _normalize_bhk(raw)
        if not k:
            continue
        by_key.setdefault(k, []).append(r)
        if k not in canonical and isinstance(raw, str) and raw.strip():
            canonical[k] = raw.strip()

    if not by_key:
        return [], {"query_bhk": bhk_input, "matched_bhk": None, "score": 0.0}

    def _extract_bedrooms(s: str) -> int | None:
        m = re.search(r"(\d+)\s*bhk", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        m = re.search(r"\d+", s)
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                return None
        return None

    def _combine_keys(keys_to_combine: list[str]) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        seen: set[int] = set()
        for k in keys_to_combine:
            for r in by_key.get(k, []):
                rid = id(r)
                if rid in seen:
                    continue
                seen.add(rid)
                combined.append(r)
        return combined

    # If user input is "generic" (e.g. "3" or "3bhk"), return ALL variants
    # for that bedroom count (3BHK + 2T, 3BHK + 3T, ...).
    q_n_generic: int | None = None
    if re.fullmatch(r"\d+", q):
        try:
            q_n_generic = int(q)
        except ValueError:
            q_n_generic = None
    elif re.fullmatch(r"\d+bhk", q):
        try:
            q_n_generic = int(q[:-3])
        except ValueError:
            q_n_generic = None

    if q_n_generic is not None:
        keys = list(by_key.keys())
        bedroom_keys = [k for k in keys if _extract_bedrooms(k) == q_n_generic]
        if bedroom_keys:
            # Prefer stable ordering: shorter variants first (e.g. "3bhk" before "3bhk+3t(soldout)")
            bedroom_keys.sort(key=lambda k: (len(k), k))
            combined = _combine_keys(bedroom_keys)
            matched_list = [canonical.get(k, k) for k in bedroom_keys]
            return combined, {"query_bhk": bhk_input, "matched_bhk": matched_list, "score": 1.0}

    # 1) Exact
    if q in by_key:
        # If user asked a "generic" BHK like "3bhk", also include variants such as
        # "3bhk+3t", "3bhk+3tvilla", etc.
        # This matches your need: input 3BHK should include 3BHK + 3T VILLA.
        is_generic = bool(re.fullmatch(r"\d+bhk", q))
        if not is_generic:
            return by_key[q], {"query_bhk": bhk_input, "matched_bhk": canonical.get(q, q), "score": 1.0}

        keys = list(by_key.keys())
        token_pat = re.compile(rf"(^|[^a-z0-9]){re.escape(q)}([^a-z0-9]|$)")
        variant_keys = [k for k in keys if q in k and token_pat.search(k)]
        combined = _combine_keys(variant_keys)
        matched_list = [canonical.get(k, k) for k in variant_keys]
        return combined, {"query_bhk": bhk_input, "matched_bhk": matched_list, "score": 1.0}

    keys = list(by_key.keys())

    # 2) Contains (e.g. "3bhk" matches "3bhk+2t")
    token_pat = re.compile(rf"(^|[^a-z0-9]){re.escape(q)}([^a-z0-9]|$)")
    contains = [k for k in keys if q in k and token_pat.search(k)]
    if contains:
        # prefer closest then shortest
        scored = [(_similarity(q, k), -len(k), k) for k in contains]
        scored.sort(reverse=True)
        best = scored[0][2]
        return by_key[best], {"query_bhk": bhk_input, "matched_bhk": canonical.get(best, best), "score": float(scored[0][0])}

    # 3) Fuzzy
    if rf_process is not None and rf_fuzz is not None:
        hit = rf_process.extractOne(
            q,
            keys,
            scorer=rf_fuzz.WRatio,
            score_cutoff=DEFAULT_BHK_FUZZY_CUTOFF * 100.0,
        )
        if hit:
            best, score_0_100, _idx = hit
            best_s = str(best)
            return by_key[best_s], {
                "query_bhk": bhk_input,
                "matched_bhk": canonical.get(best_s, best_s),
                "score": float(score_0_100) / 100.0,
            }
    else:
        candidates = difflib.get_close_matches(q, keys, n=1, cutoff=DEFAULT_BHK_FUZZY_CUTOFF)
        if candidates:
            best = candidates[0]
            score = _similarity(q, best)
            return by_key[best], {"query_bhk": bhk_input, "matched_bhk": canonical.get(best, best), "score": score}

    # 4) Bedroom-count fallback (e.g. user asks 3BHK but only 2BHK exists)
    q_n = _extract_bedrooms(bhk_input.lower())
    if q_n is not None:
        scored_n: list[tuple[int, float, str]] = []
        for k in keys:
            k_n = _extract_bedrooms(k)
            if k_n is None:
                continue
            diff = abs(q_n - k_n)
            sim = _similarity(q, k)
            scored_n.append((diff, sim, k))
        if scored_n:
            scored_n.sort(key=lambda t: (t[0], -t[1], len(t[2])))
            best = scored_n[0][2]
            score = _similarity(q, best)
            return by_key[best], {"query_bhk": bhk_input, "matched_bhk": canonical.get(best, best), "score": score}

    return [], {"query_bhk": bhk_input, "matched_bhk": None, "score": 0.0}


@app.on_event("startup")
def _startup() -> None:
    global DATA, LOCATION_INDEX, LOCATION_CANONICAL, LOCATION_KEYS
    DATA = _load_data()

    idx: dict[str, list[dict[str, Any]]] = {}
    canonical: dict[str, str] = {}

    for row in DATA:
        raw_loc = row.get("Location")
        key = _normalize_location(raw_loc)
        if not key:
            continue
        idx.setdefault(key, []).append(row)
        if key not in canonical and isinstance(raw_loc, str) and raw_loc.strip():
            canonical[key] = raw_loc.strip()

    LOCATION_INDEX = idx
    LOCATION_CANONICAL = canonical
    LOCATION_KEYS = sorted(idx.keys())


@app.get("/")
def root() -> dict[str, str]:
    """Minimal health/info route for uptime checks (e.g. Render)."""
    return {"status": "ok", "docs": "/docs"}


@app.get("/locations/{location}", response_model=None)
def get_by_location(
    location: str,
    bhk: str | None = Query(default=None, description="Optional BHK filter (supports fuzzy matching)."),
    type: str | None = Query(default=None, description="Optional Type filter (e.g. VILLA, APARTMENT)."),
    format: str | None = Query(default=None, description="Response format: json (default) or toon."),
) -> Any:
    loc_key = _normalize_location(location)
    if not loc_key:
        raise HTTPException(status_code=400, detail="location is required")

    # When called through FastAPI, `bhk` will be a string/None.
    # When called directly in Python, the default can be a `Query(...)` sentinel.
    if not isinstance(bhk, str) or not bhk.strip():
        bhk = None

    type_key: str | None = None
    if isinstance(type, str) and type.strip():
        type_key = re.sub(r"\s+", " ", type.strip().lower())

    def _maybe_format(payload: dict[str, Any]) -> dict[str, Any] | Response:
        if isinstance(format, str) and format.strip().lower() == "toon":
            return Response(content=encode_toon(payload), media_type="text/toon")
        return payload

    # 1) Exact match
    rows = LOCATION_INDEX.get(loc_key, [])
    if rows:
        filtered = rows
        if type_key:
            filtered = [
                r
                for r in filtered
                if isinstance(r.get("Type"), str) and re.sub(r"\s+", " ", r["Type"].strip().lower()) == type_key
            ]
        filtered, bhk_meta = _match_bhk(filtered, bhk) if bhk else (filtered, None)
        return _maybe_format({
            "query": location,
            "matched_location": LOCATION_CANONICAL.get(loc_key, location),
            "score": 1.0,
            "bhk": bhk_meta,
            "type": type,
            "count": len(filtered),
            "items": [_compact_item(x) for x in filtered],
        })

    # 2) Contains match (e.g. "Tambaram" => "Tambaram, Chennai")
    contains = _contains_keys(loc_key)
    if contains:
        # If multiple location keys contain the query token, return ALL of them
        # (e.g. input "Sholinganallur" should return "Sholinganallur, Chennai" and
        # any other locations that include that token).
        scored = [(_similarity(loc_key, k), -len(k), k) for k in contains[:MAX_CONTAINS_KEYS]]
        scored.sort(reverse=True)

        matched_keys = [t[2] for t in scored]
        # score = best similarity among the matched keys
        score = float(scored[0][0])

        combined_rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for k in matched_keys:
            for r in LOCATION_INDEX.get(k, []):
                rid = id(r)
                if rid in seen:
                    continue
                seen.add(rid)
                combined_rows.append(r)

        filtered = combined_rows
        if type_key:
            filtered = [
                r
                for r in filtered
                if isinstance(r.get("Type"), str) and re.sub(r"\s+", " ", r["Type"].strip().lower()) == type_key
            ]
        filtered, bhk_meta = _match_bhk(filtered, bhk) if bhk else (filtered, None)
        return _maybe_format({
            "query": location,
            "matched_location": [LOCATION_CANONICAL.get(k, k) for k in matched_keys],
            "score": score,
            "bhk": bhk_meta,
            "type": type,
            "count": len(filtered),
            "items": [_compact_item(x) for x in filtered],
        })

    # 3) Fuzzy match (typo tolerance)
    best_key, score = _best_fuzzy_key(loc_key, cutoff=DEFAULT_FUZZY_CUTOFF)
    if best_key:
        best_rows = LOCATION_INDEX.get(best_key, [])
        filtered = best_rows
        if type_key:
            filtered = [
                r
                for r in filtered
                if isinstance(r.get("Type"), str) and re.sub(r"\s+", " ", r["Type"].strip().lower()) == type_key
            ]
        filtered, bhk_meta = _match_bhk(filtered, bhk) if bhk else (filtered, None)
        return _maybe_format({
            "query": location,
            "matched_location": LOCATION_CANONICAL.get(best_key, best_key),
            "score": score,
            "bhk": bhk_meta,
            "type": type,
            "count": len(filtered),
            "items": [_compact_item(x) for x in filtered],
        })

    raise HTTPException(
        status_code=404,
        detail={
            "message": "No data found for location",
            "location": location,
            "suggestions": _top_fuzzy_keys(loc_key, n=5),
        },
    )

