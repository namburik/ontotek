import os
import json
import re
import sys
import time
import requests
import jwt
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# app/server.py lives one directory below the project root, where .env and
# the loader/ package live — add it to sys.path so `from loader...` resolves
# regardless of how this file is invoked (direct `python app/server.py`,
# `python -m app.server`, or an MCP client launching it with some other cwd).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loader.ontology_loader import build_ontology

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Initialize the OntoTek AI Data Gateway
mcp = FastMCP("OntoTek")

# Our filter-operator vocabulary -> Cube's own operator names (Cube's REST
# filter operators already match 1:1 except "ne", which Cube calls
# "notEquals").
NUMERIC_FILTER_OPERATORS = {
    "equals": "equals",
    "ne": "notEquals",
    "lt": "lt",
    "lte": "lte",
    "gt": "gt",
    "gte": "gte",
}

# Several dimensions in this dataset use an "N.LABEL" convention (e.g.
# "4.LARGECAP", "4.SP") that a person — and a small local LLM reproducing it
# from a prompt — naturally drops (just "large cap", "SP"). Rather than lean
# entirely on prompt engineering (unreliable for a 3B model), a category
# filter value that doesn't match exactly falls back to a prefix-stripped,
# case-insensitive comparison; it's only rejected if that still doesn't
# resolve to exactly one allowed value.
_CATEGORY_PREFIX_RE = re.compile(r"^\d+(\.\d+)*\.\s*")


def _normalize_category_value(v):
    return _CATEGORY_PREFIX_RE.sub("", v).strip().casefold()


def _resolve_category_value(val, allowed_values):
    """Returns the canonical allowed value matching `val`, or None."""
    if val in allowed_values:
        return val
    normalized = _normalize_category_value(val)
    matches = [av for av in allowed_values if _normalize_category_value(av) == normalized]
    return matches[0] if len(matches) == 1 else None

CUBE_API_URL = os.environ.get("CUBE_API_URL", "http://localhost:4000/cubejs-api/v1")
CUBE_API_SECRET = os.environ.get("CUBEJS_API_SECRET")

# Built live from OpenMetadata + Postgres at process startup — no generated
# file on disk. See loader/ontology_loader.py for why (scales with vocabulary size,
# not catalog size) and how (targeted per-table fetches, no full schema scan).
ONTOLOGY_CACHE = build_ontology()


def _cube_headers():
    # Cube's dev mode (CUBEJS_DEV_MODE=true) disables auth entirely, but a
    # signed token is sent whenever a secret is configured so this keeps
    # working if dev mode is ever turned off.
    if not CUBE_API_SECRET:
        return {}
    token = jwt.encode({"iat": int(time.time())}, CUBE_API_SECRET, algorithm="HS256")
    return {"Authorization": token}


@mcp.resource("schema://ontotek-graph")
def get_ontotek_ontology() -> str:
    """
    Returns the mandatory semantic context and constraints for building intents.
    The LLM must query this first to match user phrases to correct Semantic IDs.
    """
    return json.dumps(ONTOLOGY_CACHE, indent=2)


@mcp.tool()
def compile_and_execute_intent(intent_json: str) -> str:
    """
    Compiles an abstract semantic intent JSON block into a Cube query and runs
    it against Cube's semantic layer (https://cube.dev), which owns join
    resolution, aggregation, and SQL generation against Postgres.
    Input MUST be a strict JSON string mapping exclusively to verified OntoTek metric/dimension IDs.

    Example input:
    {
      "metrics": ["METRIC_AVG_CLOSE_PRICE"],
      "dimensions": ["DIM_MARKET_CAP"],
      "filters": [{"dimension": "DIM_MARKET_CAP", "value": "5.MEGACAP"}]
    }
    """
    try:
        payload = json.loads(intent_json)

        cube_measures = []
        cube_dimensions = []
        cube_filters = []

        # 1. Resolve Metrics -> Cube measures
        for metric_id in payload.get("metrics", []):
            if metric_id in ONTOLOGY_CACHE["metrics"]:
                meta = ONTOLOGY_CACHE["metrics"][metric_id]
                cube_measures.append(meta["cube_measure"])

        # 2. Resolve Dimensions -> Cube dimensions
        for dim_id in payload.get("dimensions", []):
            if dim_id in ONTOLOGY_CACHE["dimensions"]:
                meta = ONTOLOGY_CACHE["dimensions"][dim_id]
                cube_dimensions.append(meta["cube_dimension"])

        # 3. Resolve Filters with Strict Enum Matching (Anti-Injection Protection)
        # kept as an app-level check ahead of Cube, which is itself immune to
        # injection (it parameterizes filter values), so this preserves the
        # tool's documented "unmapped value" contract rather than relying on
        # Cube's error message shape.
        #
        # Two filter shapes share the "dimension" field: a DIM_* id (category
        # — exact match against a live-fetched allowed_values list) or a
        # METRIC_* id (numeric threshold — e.g. alpha < -50 — filtered on the
        # metric's own raw column via its cube_dimension, not the aggregate
        # cube_measure used when the same id appears in payload["metrics"]).
        for filter_item in payload.get("filters", []):
            dim_id = filter_item["dimension"]
            val = filter_item["value"]
            operator = filter_item.get("operator", "equals")

            if dim_id in ONTOLOGY_CACHE["dimensions"]:
                meta = ONTOLOGY_CACHE["dimensions"][dim_id]
                if meta.get("display_only"):
                    return (
                        f"OntoTek Error: '{dim_id}' is free text — it can be shown in "
                        f"results but not used as a filter."
                    )
                if operator != "equals":
                    return (
                        f"OntoTek Error: '{dim_id}' is a category — only the 'equals' "
                        f"operator is supported, not '{operator}'."
                    )
                resolved_val = _resolve_category_value(val, meta["allowed_values"])
                if resolved_val is None:
                    return f"OntoTek Error: Access denied for unmapped category filter value: '{val}'"
                # A multi_value dimension (e.g. a movie's genres) is really a
                # JSON array serialized to text in Cube (see
                # ontology_loader.py's fetch_distinct_array_values) — an
                # "equals" match would compare against the WHOLE array
                # string and never match a single element, so this uses
                # Cube's "contains" (ILIKE) instead. The user-facing contract
                # is unchanged: the model still only ever says "equals" above.
                cube_filters.append({
                    "member": meta["cube_dimension"],
                    "operator": "contains" if meta.get("multi_value") else "equals",
                    "values": [resolved_val],
                })

            elif dim_id in ONTOLOGY_CACHE["metrics"]:
                meta = ONTOLOGY_CACHE["metrics"][dim_id]
                if operator not in NUMERIC_FILTER_OPERATORS:
                    return (
                        f"OntoTek Error: '{dim_id}' is numeric — unsupported filter "
                        f"operator '{operator}'. Use one of: {sorted(NUMERIC_FILTER_OPERATORS)}."
                    )
                try:
                    float(val)
                except ValueError:
                    return f"OntoTek Error: '{dim_id}' is numeric — filter value '{val}' is not a number."
                cube_filters.append({
                    "member": meta["cube_dimension"],
                    "operator": NUMERIC_FILTER_OPERATORS[operator],
                    "values": [val],
                })

            else:
                return f"OntoTek Error: unknown filter field '{dim_id}'."

        if not cube_measures and not cube_dimensions:
            return "OntoTek Error: No valid metrics or dimensions selected."

        cube_query = {
            "measures": cube_measures,
            "dimensions": cube_dimensions,
            "filters": cube_filters,
        }

        # 4. Execute against Cube (Cube owns the Postgres connection + read-only role)
        resp = requests.get(
            f"{CUBE_API_URL}/load",
            params={"query": json.dumps(cube_query)},
            headers=_cube_headers(),
            timeout=30,
        )
        body = resp.json()
        if resp.status_code != 200 or "error" in body:
            return json.dumps({
                "status": "COMPILATION_FAILED",
                "error": body.get("error", f"Cube returned HTTP {resp.status_code}"),
            }, indent=2)

        results = body.get("data", [])
        return json.dumps({
            "status": "SUCCESS",
            "cube_query": cube_query,
            "row_count": len(results),
            "data": results,
        }, indent=2, default=str)

    except Exception as e:
        return json.dumps({
            "status": "COMPILATION_FAILED",
            "error": str(e)
        }, indent=2)


if __name__ == "__main__":
    mcp.run()
