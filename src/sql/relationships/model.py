"""The catalog as a graph (auto-schema Phase 6, step 6b): tables, columns, keys, indexes, declared
foreign keys and the join edges mined from stored procedures, all read from files that earlier sync
phases already wrote. Nothing here connects to a database or reads a data row.

Inputs (all inside ``databases/<db_id>/schema/``):
  * ``schema.json``           required. Tables, columns, primary / unique keys, indexes, declared FKs.
  * ``table_inventory.json``  optional. Row estimates and ``answering_scope`` (the one schema the SQL
                              prompts can see; see :meth:`CatalogGraph.in_answering_scope`).
  * ``sp_evidence.json``      optional. Join edges seen in stored procedures (Phase 5).

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.sql import registry
from src.sql.knowledge.loaders import validate_db_id

SCHEMA_FILE = "schema/schema.json"
INVENTORY_FILE = "schema/table_inventory.json"
EVIDENCE_FILE = "schema/sp_evidence.json"

# ---------------------------------------------------------------- column types

_INTEGER = {"int", "integer", "bigint", "smallint", "tinyint", "mediumint", "serial", "bigserial", "smallserial", "int2", "int4", "int8"}
_NUMERIC = {
    "numeric", "decimal", "number", "money", "smallmoney", "float", "double", "double precision", "real", "dec",
    "float4", "float8", "binary_float", "binary_double",
}
_STRING = {
    "varchar", "char", "nvarchar", "nchar", "text", "ntext", "tinytext", "mediumtext", "longtext", "clob", "nclob",
    "character varying", "character", "varchar2", "nvarchar2", "string", "citext", "name", "enum", "set", "bpchar", "longvarchar",
}
_BINARY = {"blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "bytea", "raw", "long raw", "image"}
_BASE = re.compile(r"[A-Za-z_][A-Za-z_ ]*")
_NOISE_WORDS = re.compile(r"\b(unsigned|signed|zerofill|identity|auto_increment)\b", re.IGNORECASE)


def type_family(raw: str | None) -> str:
    """Group a catalog column type into integer / numeric / string / uuid / datetime / date / time / bool / binary / json / other."""
    if not raw:
        return "other"
    match = _BASE.match(_NOISE_WORDS.sub(" ", str(raw)).strip())
    if not match:
        return "other"
    base = re.sub(r"\s+", " ", match.group().strip().lower())
    base = re.sub(r"\s+(with|without) time zone$", "", base)
    if base in _INTEGER:
        return "integer"
    if base in _NUMERIC:
        return "numeric"
    if base in _STRING:
        return "string"
    if base in ("uuid", "uniqueidentifier"):
        return "uuid"
    if base in ("boolean", "bool", "bit"):
        return "bool"
    if base == "date":
        return "date"
    if base.startswith("timestamp") or base in ("datetime", "datetime2", "smalldatetime", "datetimeoffset"):
        return "datetime"
    if base.startswith("time"):
        return "time"
    if base in ("json", "jsonb", "xml"):
        return "json"
    if base in _BINARY:
        return "binary"
    return "other"


def types_compatible(a: Column, b: Column) -> bool:
    """Can ``a`` reference ``b``? Same family, or integer with numeric (Oracle NUMBER). Unknown types only match identical text."""
    if a.family == "other" or b.family == "other":
        return a.type.strip().lower() == b.type.strip().lower() and bool(a.type.strip())
    return a.family == b.family or {a.family, b.family} == {"integer", "numeric"}


# ---------------------------------------------------------------- model


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    family: str
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    comment: str | None = None


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[str, ...]
    unique: bool


@dataclass(frozen=True)
class Table:
    name: str
    schema: str | None
    qualified: str
    kind: str  # "table" | "view" | anything else the catalog reports
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    indexes: tuple[Index, ...] = ()
    comment: str | None = None
    row_estimate: int | None = None

    @property
    def is_view(self) -> bool:
        return self.kind.lower() in ("view", "materialized view")

    def column(self, name: str) -> Column | None:
        low = name.lower()
        for c in self.columns:
            if c.name.lower() == low:
                return c
        return None

    def key_strength(self, column: str) -> str | None:
        """How strongly ``column`` alone identifies a row: "primary", "unique" or "unique_index"; None if it does not."""
        low = column.lower()
        if len(self.primary_key) == 1 and self.primary_key[0].lower() == low:
            return "primary"
        if any(len(k) == 1 and k[0].lower() == low for k in self.unique_keys):
            return "unique"
        if any(i.unique and len(i.columns) == 1 and i.columns[0].lower() == low for i in self.indexes):
            return "unique_index"
        return None

    def is_indexed(self, column: str) -> bool:
        """True if ``column`` is the first column of the primary key, a unique key or any index."""
        low = column.lower()
        if self.primary_key and self.primary_key[0].lower() == low:
            return True
        if any(k and k[0].lower() == low for k in self.unique_keys):
            return True
        return any(i.columns and i.columns[0].lower() == low for i in self.indexes)


@dataclass(frozen=True)
class DeclaredFK:
    """A foreign key declared in the database. ``from_table`` / ``to_table`` are qualified names present in the graph."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass(frozen=True)
class ProcEdge:
    """A join seen in stored procedures (Phase 5)."""

    a_table: str
    a_column: str
    b_table: str
    b_column: str
    statements: int
    routines: int
    confidence: str  # "high" | "low"
    declared_fk: bool


@dataclass
class CatalogGraph:
    db_id: str | None
    engine: str | None
    default_schema: str | None
    answering_schema: str | None  # the one schema the SQL prompts can see; None = unknown or no schema concept
    scope_known: bool  # True only if table_inventory.json said which schema the prompts see
    tables_outside_scope: int
    multi_schema: bool
    tables: dict[str, Table]  # keyed by qualified name, original case
    declared_fks: list[DeclaredFK] = field(default_factory=list)
    proc_edges: list[ProcEdge] = field(default_factory=list)
    inputs_hash: str = ""  # changes only when a real input changes (timestamps are ignored)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_lower = {q.lower(): q for q in self.tables}
        self._bare: dict[str, list[str]] = {}
        for q, t in self.tables.items():
            self._bare.setdefault(t.name.lower(), []).append(q)

    def table(self, name: str) -> Table | None:
        """Find a table by qualified name (any case), or by bare name when exactly one table has it."""
        low = name.lower()
        if low in self._by_lower:
            return self.tables[self._by_lower[low]]
        bare = self._bare.get(low.rsplit(".", 1)[-1], [])
        return self.tables[bare[0]] if len(bare) == 1 else None

    def in_answering_scope(self, table: Table) -> bool:
        """Can the SQL prompts see this table? They only see the connection's one schema, with bare table names."""
        if self.answering_schema is None or table.schema is None:
            return True
        return table.schema.lower() == self.answering_schema.lower()

    def name_collisions(self) -> dict[str, list[str]]:
        """Bare table names used by more than one qualified table."""
        return {n: sorted(qs) for n, qs in sorted(self._bare.items()) if len(qs) > 1}


# ---------------------------------------------------------------- loading


def _canonical_hash(*docs: dict[str, Any] | None) -> str:
    """A hash of the documents with their timestamps removed, so re-running a sync does not look like a change."""
    h = hashlib.sha256()
    for doc in docs:
        if doc is None:
            h.update(b"-")
            continue
        stable = {k: v for k, v in doc.items() if k not in ("generated_at", "generated_note")}
        h.update(json.dumps(stable, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()[:16]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _qualified(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name


def graph_from_documents(
    schema_doc: dict[str, Any],
    inventory_doc: dict[str, Any] | None = None,
    evidence_doc: dict[str, Any] | None = None,
    *,
    db_id: str | None = None,
) -> CatalogGraph | None:
    """Build the graph from already-loaded documents. None if the schema document has no tables."""
    row_estimates: dict[str, int | None] = {}
    if inventory_doc:
        for t in inventory_doc.get("tables") or []:
            key = (t.get("qualified_name") or t.get("name") or "").lower()
            if key:
                row_estimates[key] = t.get("row_estimate") if isinstance(t.get("row_estimate"), int) else None

    tables: dict[str, Table] = {}
    for t in schema_doc.get("tables") or []:
        name = t.get("name")
        if not name:
            continue
        schema = t.get("schema") or None
        qualified = t.get("qualified_name") or _qualified(schema, name)
        columns = tuple(
            Column(
                name=str(c["name"]),
                type=str(c.get("type") or ""),
                family=type_family(c.get("type")),
                nullable=bool(c.get("nullable", True)),
                is_primary_key=bool(c.get("is_primary_key", False)),
                is_foreign_key=bool(c.get("is_foreign_key", False)),
                comment=c.get("comment") or None,
            )
            for c in t.get("columns") or []
            if c.get("name")
        )
        tables[qualified] = Table(
            name=str(name),
            schema=schema,
            qualified=qualified,
            kind=str(t.get("kind") or ("view" if t.get("is_view") else "table")),
            columns=columns,
            primary_key=tuple(str(c) for c in t.get("primary_key") or []),
            unique_keys=tuple(tuple(str(c) for c in k) for k in t.get("unique_keys") or [] if k),
            indexes=tuple(
                Index(str(i.get("name") or ""), tuple(str(c) for c in i.get("columns") or []), bool(i.get("unique")))
                for i in t.get("indexes") or []
                if i.get("columns")
            ),
            comment=t.get("comment") or None,
            row_estimate=row_estimates.get(qualified.lower()),
        )
    if not tables:
        return None

    scope = (inventory_doc or {}).get("answering_scope") or {}
    graph = CatalogGraph(
        db_id=db_id,
        engine=schema_doc.get("engine"),
        default_schema=schema_doc.get("default_schema"),
        answering_schema=scope.get("schema") if inventory_doc else None,
        scope_known=bool(inventory_doc and scope),
        tables_outside_scope=int(scope.get("tables_outside_scope") or 0),
        multi_schema=bool(schema_doc.get("multi_schema")),
        tables=tables,
        inputs_hash=_canonical_hash(schema_doc, inventory_doc, evidence_doc),
    )

    skipped = 0
    for fk in schema_doc.get("relationships") or []:
        try:
            src = graph.table(_qualified(fk.get("from_schema"), fk["from_table"]))
            dst = graph.table(_qualified(fk.get("to_schema"), fk["to_table"]))
            if src is None or dst is None or src.column(fk["from_column"]) is None or dst.column(fk["to_column"]) is None:
                skipped += 1
                continue
            graph.declared_fks.append(DeclaredFK(src.qualified, src.column(fk["from_column"]).name, dst.qualified, dst.column(fk["to_column"]).name))
        except (KeyError, TypeError, AttributeError):
            skipped += 1
    if skipped:
        graph.warnings.append(f"{skipped} declared foreign keys point at tables or columns that are not in the catalog")

    for e in (evidence_doc or {}).get("join_edges") or []:
        try:
            a, b = e["a"], e["b"]
            ta, tb = graph.table(a["table"]), graph.table(b["table"])
            if ta is None or tb is None or ta.column(a["column"]) is None or tb.column(b["column"]) is None:
                continue
            graph.proc_edges.append(
                ProcEdge(ta.qualified, ta.column(a["column"]).name, tb.qualified, tb.column(b["column"]).name,
                         int(e.get("statements") or 0), int(e.get("routines") or 0), str(e.get("confidence") or "low"), bool(e.get("declared_fk")))
            )
        except (KeyError, TypeError, AttributeError):
            continue
    return graph


def load_graph(db_id: str, databases_dir: Path | None = None) -> CatalogGraph | None:
    """Read ``databases/<db_id>/schema/*.json`` into a graph. None if there is no usable ``schema.json`` (nothing to infer from)."""
    validate_db_id(db_id)
    base = (Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR) / db_id
    schema_doc = _read_json(base / SCHEMA_FILE)
    if not schema_doc:
        return None
    return graph_from_documents(
        schema_doc, _read_json(base / INVENTORY_FILE), _read_json(base / EVIDENCE_FILE), db_id=db_id
    )


__all__ = [
    "CatalogGraph", "Column", "DeclaredFK", "Index", "ProcEdge", "Table",
    "graph_from_documents", "load_graph", "type_family", "types_compatible",
]
