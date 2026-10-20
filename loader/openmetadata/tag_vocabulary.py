#!/usr/bin/env python3
"""
Ongoing convenience CLI for adding one column to the vocabulary — the normal
way to do it going forward, instead of clicking through OpenMetadata's UI or
hand-editing a local file (there isn't one to edit anymore; see
loader/ontology_loader.py's module docstring). This script holds no
vocabulary data itself, just wraps the tag/glossary-term/custom-property
PATCH calls — loader/ontology_loader.py and loader/cube/model/globals.py
pick up whatever this does on their next live fetch, no restart of
OpenMetadata needed (only of server.py / the Cube container, to refresh
THEIR in-memory snapshot).

Examples:
  # New dimension:
  python tag_vocabulary.py --table v_stock_features --column wave \\
      --term DIM_WAVE --name "wave pattern" --synonyms wave,elliott-wave

  # New metric (needs --aggregation; avg or countDistinct):
  python tag_vocabulary.py --table v_stock_features --column macd_hist \\
      --term METRIC_AVG_MACD_HIST --name "average MACD histogram" \\
      --synonyms macd,macd-histogram --aggregation avg

  # Relate two existing terms (TERM_ID:TYPE, type defaults to relatedTo):
  python tag_vocabulary.py --table v_stock_features --column bullish \\
      --term DIM_BULLISH_SIGNAL --name "bullish flag" \\
      --related DIM_GOLDEN_CROSS:broader,DIM_MACD_SIGNAL

Relation types come from OpenMetadata's own fixed enum, each one already
paired with its inverse server-side (setting "broader" on one term makes the
other read back "narrower" with no extra call) -- there's no custom
relation-type registry here, OpenMetadata already ships one:
  relatedTo, synonym, antonym, broader, narrower, partOf, hasPart,
  calculatedFrom, usedToCalculate, seeAlso

Deliberately NOT using OpenMetadata's native `parent` field for hierarchy:
setting it renests the term's own fullyQualifiedName (e.g.
OntoTekSemantics.DIM_X -> OntoTekSemantics.DIM_TREND.DIM_X), and a column's
tag reference to the old flat FQN does not cascade -- confirmed live, tagging
breaks silently (0 search hits) the moment an already-tagged term gets a
parent. `broader`/`narrower` via --related is flat (no FQN change) and gives
the same hierarchy without that landmine.

Each --related write is additive (POST .../relations for one pair at a time)
and idempotent, unlike PUTting a whole new relatedTerms array, which was
confirmed to silently replace the existing list -- never use that for
relations here, even though the plain glossaryTerm PUT below still uses PUT
for the term's own fields (name/synonyms/tags), where full-replace is fine
since the whole term body is always resent anyway.
"""
import argparse
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
load_dotenv(ROOT / ".env")

OM_HOST = os.environ["OPENMETADATA_HOST_PORT"]
OM_TOKEN = os.environ["OPENMETADATA_JWT_TOKEN"]
HEADERS = {"Authorization": f"Bearer {OM_TOKEN}", "Content-Type": "application/json"}

GLOSSARY_NAME = "OntoTekSemantics"
DEFAULT_SCHEMA_FQN = "mltek_alpha.mltek.alpha"  # the one table/schema this vocabulary uses today

# OpenMetadata's own fixed enum for glossaryTerm relations (from the 400 body
# a bad value returns) -- each already paired with its inverse server-side.
RELATION_TYPES = [
    "relatedTo", "synonym", "antonym", "broader", "narrower",
    "partOf", "hasPart", "calculatedFrom", "usedToCalculate", "seeAlso",
]


def om_get(path, **params):
    r = requests.get(f"{OM_HOST}/v1{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def om_post(path, body):
    r = requests.post(f"{OM_HOST}/v1{path}", headers=HEADERS, data=json.dumps(body), timeout=30)
    if not r.ok:
        raise RuntimeError(f"POST {path} failed: {r.status_code} {r.text}")
    return r.json()


def add_related_term(source_id, target_fqn, relation_type):
    target = om_get(f"/glossaryTerms/name/{target_fqn}")
    om_post(f"/glossaryTerms/{source_id}/relations",
            {"relationType": relation_type, "term": {"id": target["id"], "type": "glossaryTerm"}})


def om_put(path, body):
    r = requests.put(f"{OM_HOST}/v1{path}", headers=HEADERS, data=json.dumps(body), timeout=30)
    if not r.ok:
        raise RuntimeError(f"PUT {path} failed: {r.status_code} {r.text}")
    return r.json()


def om_patch_table(table_id, ops):
    patch_headers = dict(HEADERS, **{"Content-Type": "application/json-patch+json"})
    r = requests.patch(f"{OM_HOST}/v1/tables/{table_id}", headers=patch_headers, data=json.dumps(ops), timeout=30)
    if not r.ok:
        raise RuntimeError(f"PATCH /tables/{table_id} failed: {r.status_code} {r.text}")
    return r.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, help="table name, e.g. v_stock_features")
    parser.add_argument("--schema-fqn", default=DEFAULT_SCHEMA_FQN, help=f"default: {DEFAULT_SCHEMA_FQN}")
    parser.add_argument("--column", required=True)
    parser.add_argument("--term", required=True, help="DIM_* or METRIC_* id")
    parser.add_argument("--name", required=True, help="human-readable display name")
    parser.add_argument("--synonyms", default="", help="comma-separated")
    parser.add_argument("--aggregation", choices=["avg", "countDistinct"],
                         help="set this to tag a METRIC_* — omit for a DIM_*")
    parser.add_argument("--related", default="",
                         help="comma-separated TERM_ID[:TYPE] pairs, e.g. "
                              "DIM_GOLDEN_CROSS:broader,DIM_MACD_SIGNAL "
                              f"(TYPE from {RELATION_TYPES}, default relatedTo; "
                              "target terms must already exist)")
    args = parser.parse_args()

    if args.term.startswith("METRIC_") and not args.aggregation:
        parser.error("--aggregation is required for a METRIC_* term")
    if args.term.startswith("DIM_") and args.aggregation:
        parser.error("--aggregation doesn't apply to a DIM_* term")

    related = []
    for chunk in (c.strip() for c in args.related.split(",") if c.strip()):
        term_id, _, rel_type = chunk.partition(":")
        rel_type = rel_type or "relatedTo"
        if rel_type not in RELATION_TYPES:
            parser.error(f"--related: {rel_type!r} not in {RELATION_TYPES}")
        related.append((term_id, rel_type))

    om_put("/glossaries", {
        "name": GLOSSARY_NAME,
        "description": "Business terms for OntoTek metrics/dimensions, curated directly in OpenMetadata.",
    })

    # Re-PUTting a term (e.g. just to fix a typo in --name) replaces the
    # whole entity -- an absent relatedTerms key means "set to empty," not
    # "leave unchanged," confirmed live: updating DIM_GOLDEN_CROSS's own
    # synonyms silently deleted the inverse "narrower" edge DIM_BULLISH_SIGNAL
    # had pointed at it. So existing relations are read back out first and
    # restored (via the same additive/idempotent POST as --related) after
    # the PUT, rather than round-tripped through the PUT body itself, which
    # would also downgrade any typed relation (e.g. "broader") to the plain
    # "relatedTo" the create schema's untyped relatedTerms always defaults to.
    existing_relations = []  # [(target_fqn, relation_type), ...]
    try:
        current = om_get(f"/glossaryTerms/name/{GLOSSARY_NAME}.{args.term}", fields="relatedTerms")
        existing_relations = [(r["term"]["fullyQualifiedName"], r["relationType"])
                               for r in current.get("relatedTerms", [])]
    except requests.HTTPError:
        pass  # term doesn't exist yet -- nothing to preserve

    term = om_put("/glossaryTerms", {
        "glossary": GLOSSARY_NAME,
        "name": args.term,
        "displayName": args.name,
        "description": args.name,
        "synonyms": [s.strip() for s in args.synonyms.split(",") if s.strip()],
    })
    print(f"Term {args.term} ready.")

    for target_fqn, rel_type in existing_relations:
        add_related_term(term["id"], target_fqn, rel_type)

    for target_id, rel_type in related:
        add_related_term(term["id"], f"{GLOSSARY_NAME}.{target_id}", rel_type)
        print(f"Related {args.term} --{rel_type}--> {target_id}")

    fqn = f"{args.schema_fqn}.{args.table}"
    table = om_get(f"/tables/name/{fqn}", fields="tags,columns,extension")
    idx = next((i for i, c in enumerate(table["columns"]) if c["name"] == args.column), None)
    if idx is None:
        raise SystemExit(f"Column {args.column!r} not found on {fqn}")
    col = table["columns"][idx]

    term_fqn = f"{GLOSSARY_NAME}.{args.term}"
    ops = []
    existing_tags = col.get("tags") or []
    if not any(t["tagFQN"] == term_fqn for t in existing_tags):
        path = f"/columns/{idx}/tags/0" if not existing_tags else f"/columns/{idx}/tags/-"
        ops.append({
            "op": "add",
            "path": path,
            "value": {"tagFQN": term_fqn, "source": "Glossary", "labelType": "Manual", "state": "Confirmed"},
        })
    if args.aggregation and (col.get("extension") or {}).get("ontotekAggregation") != args.aggregation:
        op = "replace" if col.get("extension") else "add"
        ops.append({"op": op, "path": f"/columns/{idx}/extension", "value": {"ontotekAggregation": args.aggregation}})

    if ops:
        om_patch_table(table["id"], ops)
        print(f"Tagged {fqn}.{args.column} with {args.term}"
              + (f", ontotekAggregation={args.aggregation}" if args.aggregation else ""))
    else:
        print(f"{fqn}.{args.column} already tagged with {args.term} — nothing to do.")


if __name__ == "__main__":
    main()
