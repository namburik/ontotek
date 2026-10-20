"""Builds server.py's ONTOLOGY_CACHE live from OpenMetadata + Postgres at
process startup. Nothing here is written to disk — there is no generated
ontotek_graph.json anymore, and (as of this rewrite) no local vocabulary
file either. Every DIM_*/METRIC_* id, its (table, column) anchor, and a
metric's aggregation type are derived entirely from OpenMetadata: a column
tagged with an OntoTekSemantics.DIM_*/METRIC_* glossary term, plus — for
metrics — an `ontotekAggregation` custom property on that same column.
Curate the vocabulary by tagging/setting that property directly in
OpenMetadata (loader/openmetadata/tag_vocabulary.py is a thin CLI wrapper
for the PATCH calls that does), not by editing Python/JSON here. This
closes the drift risk the previous design had: DIMENSION_ANCHORS (this
file), metric_overrides.json, dimension_metadata.json, and
seed_glossary.py's binding dicts were four hand-maintained copies of
overlapping facts, with nothing enforcing they agreed with each other.

Scales with vocabulary size, not catalog size: one bounded search query
(`tags.tagFQN:OntoTekSemantics.*` against OpenMetadata's search index)
returns every tagged table+column in a single call — cost is O(number of
tagged columns), never O(total tables in the catalog), the same property
the old hand-maintained DIMENSION_ANCHORS dict and metric_overrides.json
gave, now without either needing to exist. The "joins" field earlier
versions produced is gone entirely: it was never read anywhere in
server.py — Cube resolves joins itself from its own YAML model files
(loader/cube/model/cubes/*.yml).

The search index can lag behind a hard-deleted entity (observed directly:
a table removed via the API stayed searchable for a while after) — a
search hit whose direct entity fetch 404s is skipped rather than trusted.
It does NOT lag on tag/extension changes made via a normal PATCH (also
observed directly: those propagate immediately), so no such workaround is
needed for those.

Names/synonyms are read live from OpenMetadata's OntoTekSemantics glossary,
authored directly on the glossary term (in OpenMetadata's UI, or via
tag_vocabulary.py) — no local seed file. If a glossary term is missing
(never authored, or renamed), name/synonyms fall back to a derived default
rather than failing — this is metadata richness, not something query
correctness depends on.

allowed_values: still a live, targeted `SELECT DISTINCT ...` per dimension
against Postgres directly (same deliberate deviation from OpenMetadata's
profiler as before — see the design note in README.md).
"""
import os

import psycopg2
import requests
from dotenv import load_dotenv

_HERE = __import__("pathlib").Path(__file__).resolve().parent  # this file's own loader/ directory
# One level up, to the project root, where .env lives.
ROOT = _HERE.parent
load_dotenv(ROOT / ".env")

OM_HOST = os.environ["OPENMETADATA_HOST_PORT"]
OM_TOKEN = os.environ["OPENMETADATA_JWT_TOKEN"]
PG_URI = os.environ["POSTGRES_READ_ONLY_URI"]
HEADERS = {"Authorization": f"Bearer {OM_TOKEN}"}

GLOSSARY_NAME = "OntoTekSemantics"

# OpenMetadata SERVICE name (the first segment of a table's
# fullyQualifiedName, e.g. "mltek_alpha.mltek.alpha.v_stock_features") ->
# Postgres connection URI. This is the one thing genuinely NOT derivable
# from the catalog itself (OpenMetadata's own service connection config
# holds credentials, not something to read via this token) — every other
# vocabulary fact comes from tags/glossary/custom-properties above.
SOURCES = {
    "mltek_alpha": PG_URI,  # alpha lives in the same `mltek` Postgres database as ontotek
    "novatek": os.environ["NOVATEK_READ_ONLY_URI"],  # separate database, same local cluster
}


def om_get(path, **params):
    r = requests.get(f"{OM_HOST}/v1{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _table_exists(fqn):
    try:
        om_get(f"/tables/name/{fqn}")
        return True
    except requests.HTTPError:
        return False


def fetch_tagged_columns():
    """One bounded search call -> every column tagged under OntoTekSemantics,
    split into dim_anchors ({dim_id: (table_fqn, column, domain, is_array,
    is_free_text)})
    and metric_anchors ({metric_id: (table_fqn, column, aggregation, domain)}).
    `domain` is the table's assigned OpenMetadata Domain name (e.g.
    "Equities", "Movies"), or None if untagged — the search hit's own
    `domains` field already carries this, no extra per-table fetch needed.
    `is_array` (dimensions only) is True for a JSON/array-typed column (e.g.
    a movie's `genres` list) — see build_dimensions/fetch_distinct_values for
    why that needs different SQL to get individual allowed values instead of
    whole-array rows. `is_free_text` (dimensions only) is True for a TEXT-typed
    column (e.g. a movie's `description`) — unbounded prose, so it has no
    enumerable allowed_values and is display-only (see build_dimensions). Hits for a table that no longer exists (search-index
    lag on hard deletes — see module docstring) are skipped, checked once
    per unique table."""
    hits = om_get(
        "/search/query", q=f"tags.tagFQN:{GLOSSARY_NAME}.*", index="table_search_index", size=200
    )["hits"]["hits"]

    dim_anchors, metric_anchors = {}, {}
    checked_tables = {}
    prefix = f"{GLOSSARY_NAME}."
    for hit in hits:
        src = hit["_source"]
        fqn = src["fullyQualifiedName"]
        if fqn not in checked_tables:
            checked_tables[fqn] = _table_exists(fqn)
        if not checked_tables[fqn]:
            continue
        domains = src.get("domains") or []
        domain = domains[0]["name"] if domains else None

        for col in src.get("columns", []):
            for tag in col.get("tags", []):
                tag_fqn = tag.get("tagFQN", "")
                if not tag_fqn.startswith(prefix):
                    continue
                term_id = tag_fqn[len(prefix):]
                if term_id.startswith("DIM_"):
                    is_array = col.get("dataType") == "JSON"
                    is_free_text = col.get("dataType") == "TEXT"
                    dim_anchors[term_id] = (fqn, col["name"], domain, is_array, is_free_text)
                elif term_id.startswith("METRIC_"):
                    aggregation = (col.get("extension") or {}).get("ontotekAggregation")
                    if aggregation is None:
                        print(f"  WARNING: {fqn}.{col['name']} tagged {term_id} but has no "
                              f"ontotekAggregation custom property set, skipping")
                        continue
                    metric_anchors[term_id] = (fqn, col["name"], aggregation, domain)
    return dim_anchors, metric_anchors


def fetch_glossary_terms():
    """term_id -> {"name": displayName, "synonyms": [...], "relations": {...}},
    for every term under OntoTekSemantics — including a term with no column
    tag at all (a pure concept, e.g. a broader grouping term nothing is
    anchored to). One bulk call — bounded by vocabulary size (number of
    curated terms), not catalog size.

    "relations" is {relation_type: [{"id": term_id, "name": display_name},
    ...]}, built from OpenMetadata's typed relatedTerms (e.g. "broader":
    [{"id": "DIM_GOLDEN_CROSS", "name": "golden cross flag"}]) — the name is
    resolved in a second pass once every term's own name is known, so the
    system prompt can show a human-readable relation target even when that
    target is a pure concept absent from ONTOLOGY_CACHE's dimensions/metrics
    (see app/ontotek_agent.py's _build_system_prompt).

    Deliberately not using OpenMetadata's native `parent` field for hierarchy
    — see tag_vocabulary.py's module docstring for why (it renests a term's
    own fullyQualifiedName, silently breaking its column tag reference).
    `broader`/`narrower` via relatedTerms gives the same hierarchy flat, and
    OpenMetadata already auto-mirrors each type to its inverse server-side."""
    data = om_get("/glossaryTerms", limit=200, fields="relatedTerms")
    prefix = f"{GLOSSARY_NAME}."

    raw = {}  # term_id -> (name, synonyms, [(relation_type, other_term_id), ...])
    for term in data["data"]:
        fqn = term["fullyQualifiedName"]
        if not fqn.startswith(prefix):
            continue
        term_id = fqn[len(prefix):]
        raw_relations = [
            (rel["relationType"], rel["term"]["fullyQualifiedName"][len(prefix):])
            for rel in term.get("relatedTerms", [])
            if rel["term"]["fullyQualifiedName"].startswith(prefix)
        ]
        raw[term_id] = (term.get("displayName", term_id), term.get("synonyms", []), raw_relations)

    result = {}
    for term_id, (name, synonyms, raw_relations) in raw.items():
        relations = {}
        for rel_type, other_id in raw_relations:
            other_name = raw.get(other_id, (other_id,))[0]
            relations.setdefault(rel_type, []).append({"id": other_id, "name": other_name})
        result[term_id] = {"name": name, "synonyms": synonyms, "relations": relations}
    return result


def broader_chain(term_id, glossary_terms):
    """Transitive closure of a term's "broader" edges (Dallas -> Texas -> USA
    style walk), computed here in Python rather than by OpenMetadata — it has
    no query-time transitive-closure primitive for this, and at vocabulary
    scale a plain walk is simpler than reaching for one. `seen` guards
    against a cycle a human could accidentally create by hand-curating in
    OpenMetadata (nothing server-side prevents A broader B broader A). Only
    follows the first "broader" edge when a term has more than one — good
    enough for the demo-scale hierarchies this vocabulary has today; a term
    with genuinely multiple broader parents would need every path walked,
    not just one."""
    chain = []
    seen = {term_id}
    current = term_id
    while True:
        parents = glossary_terms.get(current, {}).get("relations", {}).get("broader", [])
        if not parents:
            return chain
        parent = parents[0]["id"]
        if parent in seen:
            return chain
        chain.append(parent)
        seen.add(parent)
        current = parent


def to_cube_name(table_name):
    """movietek_catalog -> MovietekCatalog (must match cube/model/cubes/*.yml)."""
    return "".join(part.capitalize() for part in table_name.split("_"))


def to_cube_member(column_name):
    """genre_name -> genreName (must match cube/model/cubes/*.yml)."""
    parts = column_name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _pascal(column_name):
    member = to_cube_member(column_name)
    return member[0].upper() + member[1:]


def fetch_distinct_values(cur, schema, table, column, limit=50):
    # Stringified uniformly: server.py's filter-value check (`val in
    # allowed_values`) always compares against a JSON string from the LLM, so
    # a boolean column (e.g. goldencross) returning Python True/False here
    # instead of "True"/"False" would silently make that filter unmatchable.
    query = f"SELECT DISTINCT {column} FROM {schema}.{table} WHERE {column} IS NOT NULL ORDER BY 1 LIMIT %s"
    cur.execute(query, (limit,))
    return [str(row[0]) for row in cur.fetchall()]


def fetch_distinct_array_values(cur, schema, table, column, limit=50):
    """Like fetch_distinct_values, but for a JSON array column (e.g. a
    movie's `genres`: ["Action", "Sci-Fi"]) — DISTINCT on the raw column
    would return whole arrays, never an individual value a filter could ever
    match, so this unnests each array first via jsonb_array_elements_text."""
    query = (
        f"SELECT DISTINCT jsonb_array_elements_text({column}) FROM {schema}.{table} "
        f"WHERE {column} IS NOT NULL ORDER BY 1 LIMIT %s"
    )
    cur.execute(query, (limit,))
    return [str(row[0]) for row in cur.fetchall()]


def _parse_table_fqn(fqn):
    """service.database.schema.table -> (service, schema, table)."""
    service, _database, schema, table = fqn.split(".")
    return service, schema, table


def build_dimensions(dim_anchors, glossary_terms, cursors):
    dimensions = {}
    for dim_id, (table_fqn, column_name, domain, is_array, is_free_text) in dim_anchors.items():
        service, schema, table_name = _parse_table_fqn(table_fqn)
        term = glossary_terms.get(dim_id, {})
        # A free-text column (e.g. a movie description) has one near-unique
        # value per row — no meaningful allowed_values list, and dumping
        # hundreds of paragraphs into the LLM prompt would blow its context.
        # It's requestable in "dimensions" only, never filterable.
        if is_free_text:
            allowed_values = []
        else:
            fetch = fetch_distinct_array_values if is_array else fetch_distinct_values
            allowed_values = fetch(cursors[service], schema, table_name, column_name)

        relations = dict(term.get("relations", {}))
        chain = broader_chain(dim_id, glossary_terms)
        if chain:
            relations["broader_chain"] = chain

        dimensions[dim_id] = {
            "name": term.get("name", dim_id.lower().replace("_", " ")),
            "synonyms": term.get("synonyms", []),
            "domain": domain,
            # True for a JSON array column (e.g. genres) whose Cube dimension
            # is really the array serialized to text — server.py's filter
            # compiler must use a "contains" match instead of "equals" for
            # these, since an exact match against the serialized array would
            # never match a single element.
            "multi_value": is_array,
            # True for a free-text column: shown in results but rejected as a
            # filter field (server.py) and listed without values in the prompt.
            "display_only": is_free_text,
            "table": table_name,
            "column": column_name,
            "allowed_values": allowed_values,
            "cube_dimension": f"{to_cube_name(table_name)}.{to_cube_member(column_name)}",
            "relations": relations,
        }
    return dimensions


def build_metrics(metric_anchors, glossary_terms):
    metrics = {}
    for metric_id, (table_fqn, column_name, aggregation, domain) in metric_anchors.items():
        _service, _schema, table_name = _parse_table_fqn(table_fqn)
        term = glossary_terms.get(metric_id, {})
        measure_name = aggregation + _pascal(column_name)  # e.g. "avg"+"Rsi", "countDistinct"+"Symbol"
        sql_formula = (
            f"COUNT(DISTINCT {table_name}.{column_name})" if aggregation == "countDistinct"
            else f"AVG({table_name}.{column_name})"
        )

        relations = dict(term.get("relations", {}))
        chain = broader_chain(metric_id, glossary_terms)
        if chain:
            relations["broader_chain"] = chain

        metrics[metric_id] = {
            "name": term.get("name", metric_id.lower().replace("_", " ")),
            "synonyms": term.get("synonyms", []),
            "domain": domain,
            "formula": sql_formula,
            "table": table_name,
            "column": column_name,
            "requires_aggregation": True,
            "cube_measure": f"{to_cube_name(table_name)}.{measure_name}",
            "cube_dimension": f"{to_cube_name(table_name)}.{to_cube_member(column_name)}",
            "relations": relations,
        }
    return metrics


def build_ontology():
    """The only entry point server.py needs. Returns {"metrics": ..., "dimensions": ...} —
    fresh from OpenMetadata + Postgres, nothing cached on disk, nothing hand-typed locally."""
    dim_anchors, metric_anchors = fetch_tagged_columns()
    glossary_terms = fetch_glossary_terms()

    services_needed = {_parse_table_fqn(fqn)[0] for fqn, *_ in dim_anchors.values()}
    cursors = {}
    conns = []
    try:
        for service in services_needed:
            conn = psycopg2.connect(SOURCES[service])
            conn.set_session(readonly=True, autocommit=True)
            conns.append(conn)
            cursors[service] = conn.cursor()
        dimensions = build_dimensions(dim_anchors, glossary_terms, cursors)
    finally:
        for conn in conns:
            conn.close()

    metrics = build_metrics(metric_anchors, glossary_terms)
    return {"metrics": metrics, "dimensions": dimensions}


if __name__ == "__main__":
    import json

    print(json.dumps(build_ontology(), indent=2))
