"""Governed write path for OntoTek -- the DML counterpart to
compile_and_execute_intent's read-only Cube queries. A caller (an LLM-built
intent, or anything else) never touches raw SQL or an arbitrary
table/column name here: it can only name one of a small, hand-curated
MUTATIONS entries, each scoped to one table, one operation, and an
allowlist of writable columns -- the same discipline as ONTOLOGY_CACHE's
fixed DIM_*/METRIC_* vocabulary on the read side, extended with what a
write additionally needs: an explicit confirm step before anything
executes, and an audit trail of what actually changed.

Needs its own writable Postgres connection -- POSTGRES_READ_ONLY_URI
(ontology_loader.py, server.py) is deliberately read-only and must not be
reused here. Not wired into server.py's MCP tool set: exposing this to an
agent is a separate, conscious decision, not something that falls out of
importing this module.
"""
import time
from dataclasses import dataclass
from typing import Any, Optional

from psycopg2 import sql


@dataclass(frozen=True)
class MutationSpec:
    """One entry in MUTATIONS: everything needed to build and validate a
    single INSERT/UPDATE/DELETE against exactly one table."""
    table: str
    operation: str  # "insert", "update", or "delete" -- this spec allows only this one
    writable_columns: tuple  # columns a caller may set; anything else is rejected
    key_column: Optional[str] = None  # required for update/delete: identifies the row


# Hand-curated, reviewed catalog -- replace these with real entries for
# whatever tables should be writable. A caller can only ever reference a
# mutation_id here; there is no path from caller input to an arbitrary
# table or column name.
MUTATIONS = {
    "UPDATE_ORDER_STATUS": MutationSpec(
        table="orders", operation="update",
        writable_columns=("status",), key_column="order_id",
    ),
    "INSERT_ORDER_NOTE": MutationSpec(
        table="order_notes", operation="insert",
        writable_columns=("order_id", "note", "author"),
    ),
    "DELETE_ORDER_NOTE": MutationSpec(
        table="order_notes", operation="delete",
        writable_columns=(), key_column="note_id",
    ),
}


class MutationRejected(Exception):
    """Raised by validation -- unknown mutation_id, wrong operation for the
    method called, disallowed column, missing key/confirm. Kept distinct
    from a DB error so a caller can tell "your request was invalid" apart
    from "the database itself failed"."""


class MutationExecutor:
    """One instance per caller-supplied connection. `conn` must be a
    writable, non-autocommit psycopg2 connection -- autocommit off is what
    makes confirm=False (preview) safe: nothing is visible to any other
    session, and nothing can be half-applied, until the CALLER runs
    conn.commit(). This class never commits or rolls back on its own, since
    a caller may need several mutations in one transaction."""

    def __init__(self, conn):
        self._conn = conn
        self._audit: list = []

    def insert(self, mutation_id: str, values: dict, confirm: bool = False) -> dict:
        spec = self._spec_for(mutation_id, "insert")
        self._check_columns(mutation_id, spec, values)
        if not values:
            raise MutationRejected(f"{mutation_id}: at least one column value is required.")
        columns = list(values)
        stmt = sql.SQL("INSERT INTO {table} ({cols}) VALUES ({vals})").format(
            table=sql.Identifier(spec.table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        params = [values[c] for c in columns]
        return self._run(mutation_id, spec, stmt, params, confirm, key_value=None, values=values)

    def update(self, mutation_id: str, key_value: Any, values: dict, confirm: bool = False) -> dict:
        spec = self._spec_for(mutation_id, "update")
        self._check_columns(mutation_id, spec, values)
        if not values:
            raise MutationRejected(f"{mutation_id}: at least one column value is required.")
        if key_value is None:
            raise MutationRejected(f"{mutation_id}: key_value is required for an update.")
        columns = list(values)
        stmt = sql.SQL("UPDATE {table} SET {assignments} WHERE {key} = %s").format(
            table=sql.Identifier(spec.table),
            assignments=sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns
            ),
            key=sql.Identifier(spec.key_column),
        )
        params = [values[c] for c in columns] + [key_value]
        return self._run(mutation_id, spec, stmt, params, confirm, key_value=key_value, values=values)

    def delete(self, mutation_id: str, key_value: Any, confirm: bool = False) -> dict:
        spec = self._spec_for(mutation_id, "delete")
        if key_value is None:
            raise MutationRejected(f"{mutation_id}: key_value is required for a delete.")
        stmt = sql.SQL("DELETE FROM {table} WHERE {key} = %s").format(
            table=sql.Identifier(spec.table), key=sql.Identifier(spec.key_column),
        )
        return self._run(mutation_id, spec, stmt, [key_value], confirm, key_value=key_value, values={})

    def audit_log(self) -> list:
        return list(self._audit)

    def _spec_for(self, mutation_id: str, operation: str) -> MutationSpec:
        spec = MUTATIONS.get(mutation_id)
        if spec is None:
            raise MutationRejected(f"Unknown mutation_id '{mutation_id}'.")
        if spec.operation != operation:
            raise MutationRejected(
                f"'{mutation_id}' is a {spec.operation}, not a {operation} -- "
                f"call .{spec.operation}() instead."
            )
        return spec

    def _check_columns(self, mutation_id, spec, values):
        unknown = set(values) - set(spec.writable_columns)
        if unknown:
            raise MutationRejected(
                f"{mutation_id}: column(s) {sorted(unknown)} are not writable -- "
                f"allowed: {spec.writable_columns}."
            )

    def _run(self, mutation_id, spec, stmt, params, confirm, key_value, values) -> dict:
        # Dry-run by default: returns the exact compiled SQL/params for a
        # caller (human or agent) to review, same purpose as server.py
        # showing "generated intent" before compile_and_execute_intent runs
        # -- except here nothing executes until the caller re-invokes with
        # confirm=True.
        if not confirm:
            return {
                "status": "PREVIEW", "mutation_id": mutation_id, "operation": spec.operation,
                "sql": stmt.as_string(self._conn), "params": params,
            }
        with self._conn.cursor() as cur:
            cur.execute(stmt, params)
            row_count = cur.rowcount
        self._audit.append({
            "mutation_id": mutation_id, "operation": spec.operation, "table": spec.table,
            "key_value": key_value, "values": values, "row_count": row_count, "ts": time.time(),
        })
        return {"status": "EXECUTED", "mutation_id": mutation_id, "row_count": row_count}
