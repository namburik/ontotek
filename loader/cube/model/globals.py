"""Dynamically generates Cube's data model from OpenMetadata's live catalog
at Cube's own startup/recompile — no hand-maintained YAML file per table.
Rendered by cubes/dynamic.yml.jinja via the `load_cubes()` Jinja function
this registers.

Runs inside Cube's own Python sandbox, which has neither `requests` nor
`psycopg2` installed (verified directly in the running container) — so this
uses only stdlib `urllib`, and only talks to OpenMetadata (column/FK metadata),
never Postgres directly. That's enough: Cube's model only needs column
names/types, not live data values (unlike ontology_loader.py's allowed_values,
which server.py's Python process fetches separately, with psycopg2 available).

Auto-derived: cube name, sql_table, one dimension per column (type mapped
from OpenMetadata's dataType; JSON columns skipped), a `count` measure,
joins from enforced FK constraints, and the primary key — including tables
with no single-column PK: a composite PRIMARY_KEY or, failing that, a UNIQUE
constraint gets synthesized into a `CONCAT(...)` key dimension automatically
— no per-table config needed, this is mechanical once you look at
tableConstraints instead of only per-column `constraint` markers.

Metric measures (avg/countDistinct on a specific column) are ALSO
auto-derived now, from the same OntoTekSemantics tags + `ontotekAggregation`
custom property that loader/ontology_loader.py reads for the agent's own
vocabulary — see that file's module docstring for the full rationale
(single source of truth in OpenMetadata, no local vocabulary file). What's
still NOT derivable this way — joins that aren't enforced FK constraints,
columns to exclude, and a dimension over a column type TYPE_MAP skips (e.g.
JSON) — comes from the hand-curated cube_overlays.json sitting next to this
file, via `extra_joins`/`extra_dimensions` (both combined with, not
replacing, the auto-derived ones); a truly multi-column/conditional metric
formula that doesn't fit the (column, aggregation) shape would go there too,
via `extra_measures`.

Scoped to `mltek/alpha.v_stock_features` (real stock feature data, same
Postgres database as ontotek → default Cube datasource, no `data_source:`
needed) and `novatek.public.movie_trends` (separate Postgres database on the
same local cluster → its own `data_source: novatek`, see SCHEMA_INFO).
"""
import json
import os
import urllib.request

from cube import TemplateContext

template = TemplateContext()

OM_HOST = os.environ.get("CUBEJS_OM_INTERNAL_HOST", "http://openmetadata-server:8585/api")
OM_TOKEN = os.environ.get("CUBEJS_OM_TOKEN", "")

# OpenMetadata dataType -> Cube dimension type. Anything not listed (JSON, etc.)
# is skipped rather than guessed.
TYPE_MAP = {
    "INT": "number", "BIGINT": "number", "SMALLINT": "number",
    "NUMERIC": "number", "DECIMAL": "number", "DOUBLE": "number", "FLOAT": "number",
    "VARCHAR": "string", "CHAR": "string", "TEXT": "string",
    "DATE": "time", "TIMESTAMP": "time", "DATETIME": "time",
    "BOOLEAN": "boolean",
}

SOURCES = {
    "alpha": "mltek_alpha.mltek.alpha",
    "novatek": "novatek.novatek.public",
}
# source_key -> (Postgres schema name for sql_table, Cube data_source name or
# "" for the default datasource). alpha lives in the same `mltek` Postgres
# database as ontotek, so it shares the default datasource/connection.
# novatek is a separate Postgres database (same local cluster) — needs its
# own Cube data_source, wired up in docker-compose.yml's CUBEJS_DS_NOVATEK_*.
SCHEMA_INFO = {
    "alpha": ("alpha", ""),
    "novatek": ("public", "novatek"),
}
TABLES = {
    "alpha": ["v_stock_features"],
    "novatek": ["movie_trends"],
}

# __file__ isn't a real filesystem path in Cube's Python model executor (just
# the bare filename), so this can't be derived relative to it — hardcoded to
# the fixed model root docker-compose.yml mounts loader/cube/model to.
_OVERLAYS_PATH = "/cube/conf/model/cube_overlays.json"
OVERLAYS = json.load(open(_OVERLAYS_PATH))


def om_get(path):
    req = urllib.request.Request(
        f"{OM_HOST}/v1{path}", headers={"Authorization": f"Bearer {OM_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def to_cube_name(table_name):
    """movietek_catalog -> MovietekCatalog."""
    return "".join(part.capitalize() for part in table_name.split("_"))


def to_cube_member(column_name):
    """genre_name -> genreName."""
    parts = column_name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _pascal(column_name):
    member = to_cube_member(column_name)
    return member[0].upper() + member[1:]


def _build_extra_measures(columns):
    """A METRIC_*-tagged column with an ontotekAggregation custom property
    becomes one measure, named identically to how ontology_loader.py derives
    the same metric's cube_measure (aggregation + PascalCase(column)) — the
    two MUST agree, since server.py's compile_and_execute_intent sends the
    name ontology_loader.py computed straight into a Cube query."""
    measures = []
    for col in columns:
        aggregation = (col.get("extension") or {}).get("ontotekAggregation")
        if not aggregation:
            continue
        is_metric_tagged = any(
            t.get("tagFQN", "").startswith("OntoTekSemantics.METRIC_") for t in col.get("tags", [])
        )
        if not is_metric_tagged:
            continue
        measures.append({
            "name": aggregation + _pascal(col["name"]),
            "sql": col["name"],
            "type": aggregation,
        })
    return measures


def _find_key_columns(table_constraints):
    """Columns to build a primary key from, when no single column already
    carries OpenMetadata's per-column PRIMARY_KEY marker: a composite
    PRIMARY_KEY constraint if there is one, else a UNIQUE constraint
    (Postgres tables sometimes have the latter with no PK at all)."""
    constraints = table_constraints or []
    for constraint in constraints:
        if constraint.get("constraintType") == "PRIMARY_KEY":
            return constraint["columns"]
    for constraint in constraints:
        if constraint.get("constraintType") == "UNIQUE":
            return constraint["columns"]
    return None


def _build_dimensions(columns, table_constraints, excluded):
    dimensions = []
    has_single_column_pk = any(c.get("constraint") == "PRIMARY_KEY" for c in columns)

    for col in columns:
        if col["name"] in excluded:
            continue
        cube_type = TYPE_MAP.get(col["dataType"])
        if cube_type is None:
            continue
        dimensions.append({
            "name": to_cube_member(col["name"]),
            "sql": col["name"],
            "type": cube_type,
            "primary_key": col.get("constraint") == "PRIMARY_KEY",
            "shown": True,
        })

    if not has_single_column_pk:
        key_columns = _find_key_columns(table_constraints)
        if key_columns:
            concat_sql = ", '_', ".join(f"{{CUBE}}.{c}" for c in key_columns)
            dimensions.insert(0, {
                "name": "syntheticKey",
                "sql": f"CONCAT({concat_sql})",
                "type": "string",
                "primary_key": True,
                "shown": False,
            })

    return dimensions


def _build_fk_joins(table_constraints):
    joins = []
    for constraint in table_constraints or []:
        if constraint.get("constraintType") != "FOREIGN_KEY":
            continue
        for col, ref_col in zip(constraint["columns"], constraint["referredColumns"]):
            ref_table = ref_col.split(".")[-2]
            ref_column = ref_col.split(".")[-1]
            joins.append({
                "name": to_cube_name(ref_table),
                "sql": f"{{CUBE}}.{col} = {{{to_cube_name(ref_table)}}}.{ref_column}",
                "relationship": "many_to_one",
            })
    return joins


@template.function("load_cubes")
def load_cubes():
    cubes = []
    for source_key, table_names in TABLES.items():
        schema_fqn = SOURCES[source_key]
        pg_schema, data_source = SCHEMA_INFO[source_key]
        for table_name in table_names:
            data = om_get(f"/tables/name/{schema_fqn}.{table_name}?fields=columns,tableConstraints,extension,tags")
            cube_name = to_cube_name(table_name)
            overlay = OVERLAYS.get(cube_name, {})
            table_constraints = data.get("tableConstraints")

            dimensions = _build_dimensions(
                data["columns"], table_constraints, set(overlay.get("exclude_columns", []))
            ) + list(overlay.get("extra_dimensions", []))
            joins = _build_fk_joins(table_constraints) + list(overlay.get("extra_joins", []))
            measures = (
                [{"name": "count", "type": "count"}]
                + _build_extra_measures(data["columns"])
                + list(overlay.get("extra_measures", []))
            )

            cubes.append({
                "name": cube_name,
                "sql_table": f"{pg_schema}.{table_name}",
                "data_source": data_source,
                "dimensions": dimensions,
                "joins": joins,
                "measures": measures,
            })
    return cubes
