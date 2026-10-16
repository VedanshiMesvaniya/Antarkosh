"""Read a database's catalog with SQLAlchemy's Inspector, across ALL schemas it can see.

``reflect_database`` returns plain, JSON-serialisable data (no SQLAlchemy objects):

    {"dialect", "default_schema", "schemas": {schema: {"tables": {table: {...}}}},
     "warnings": [...], "skipped_schemas": {schema: reason}}

Only catalog metadata is read. A schema is included when it is not a system schema, holds at
least one table or view this account can read, and (if the caller named schemas) was asked for.
SQLAlchemy lists objects the account may not be allowed to read (PostgreSQL shows every
table in pg_class), so for PostgreSQL the list is narrowed to schemas the account may USE and
tables it may SELECT. Every Inspector call is isolated: one failing call becomes a warning, not a failure.

Origin: new (no previous file)
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine.reflection import Inspector, ObjectKind

logger = logging.getLogger(__name__)

_MYSQL_SYSTEM = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
_MSSQL_SYSTEM = frozenset(
    s.lower()
    for s in (
        "sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin",
        "db_backupoperator", "db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter",
    )
)
# Oracle-maintained accounts, used only when the ORACLE_MAINTAINED column (12c+) is not available.
_ORACLE_SYSTEM = frozenset(
    {
        "SYS", "SYSTEM", "XDB", "CTXSYS", "MDSYS", "OUTLN", "DBSNMP", "APPQOSSYS", "AUDSYS", "GSMADMIN_INTERNAL",
        "LBACSYS", "OJVMSYS", "OLAPSYS", "ORDDATA", "ORDSYS", "ORDPLUGINS", "WMSYS", "DVSYS", "DVF", "ANONYMOUS",
        "REMOTE_SCHEDULER_AGENT", "SI_INFORMTN_SCHEMA", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "XS$NULL",
        "GGSYS", "DBSFWUSER", "GSMCATUSER", "GSMUSER", "ORACLE_OCM", "PDBADMIN", "EXFSYS", "DIP", "MDDATA",
        "APEX_PUBLIC_USER", "FLOWS_FILES", "SPATIAL_CSW_ADMIN_USR", "SPATIAL_WFS_ADMIN_USR", "SYS$UMF",
    }
)


def is_system_schema(dialect_key: str, name: str) -> bool:
    lower = name.lower()
    if dialect_key == "postgresql":
        return lower == "information_schema" or lower.startswith("pg_")
    if dialect_key == "mysql":
        return lower in _MYSQL_SYSTEM
    if dialect_key == "mssql":
        return lower in _MSSQL_SYSTEM
    if dialect_key == "oracle":
        return name.upper() in _ORACLE_SYSTEM
    return False


def _type_text(coltype: Any, dialect: Any) -> str:
    """The column's own database type (for an enum, its type name), not SQLAlchemy's generic rendering."""
    try:
        return str(coltype.compile(dialect=dialect))
    except Exception:  # noqa: BLE001
        try:
            return str(coltype)
        except Exception:  # noqa: BLE001
            return type(coltype).__name__


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _column(col: dict[str, Any], dialect: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": str(col["name"]),
        "type": _type_text(col.get("type"), dialect),
        "nullable": col["nullable"] if isinstance(col.get("nullable"), bool) else None,
        "default": _clean(col.get("default")),
        "auto_increment": col["autoincrement"] if isinstance(col.get("autoincrement"), bool) else None,
        "comment": _clean(col.get("comment")),
    }
    enums = getattr(col.get("type"), "enums", None)
    if enums:
        out["enum_values"] = [str(v) for v in enums]
    return out


class _Collector:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def call(self, label: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except NotImplementedError:
            return default
        except Exception as exc:  # noqa: BLE001 - one failing Inspector call must not lose the rest
            self.warnings.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
            return default


def reflect_with_inspector(
    insp: Inspector,
    dialect_key: str,
    schemas: list[str] | None = None,
    accessible: set[tuple[str, str]] | None = None,
    candidate_schemas: list[str] | None = None,
) -> dict[str, Any]:
    """The reflection logic, separate from how the Inspector was obtained (so it can be tested with a fake)."""
    col = _Collector()
    default_schema = insp.default_schema_name
    out: dict[str, Any] = {
        "dialect": dialect_key,
        "default_schema": default_schema,
        "schemas": {},
        "warnings": col.warnings,
        "skipped_schemas": {},
    }

    if dialect_key == "sqlite":
        names, chosen = ["main"], ["main"]
    else:
        names = candidate_schemas if candidate_schemas is not None else col.call("schema list", insp.get_schema_names, [])
        by_lower: dict[str, list[str]] = {}
        for n in names:
            by_lower.setdefault(n.lower(), []).append(n)
        for n in names:
            if is_system_schema(dialect_key, n):
                out["skipped_schemas"][n] = "system schema"
        if schemas:
            chosen = []
            for want in schemas:
                candidates_for = [want] if want in names else by_lower.get(want.lower(), [])
                if len(candidates_for) == 1:
                    chosen.append(candidates_for[0])
                else:
                    col.warnings.append(f"requested schema {want!r} was not found")
        else:
            chosen = [n for n in names if not is_system_schema(dialect_key, n)]

    accessible_by_schema: dict[str, set[str]] | None = None
    if accessible is not None:
        accessible_by_schema = {}
        for s, t in accessible:
            accessible_by_schema.setdefault(s, set()).add(t)

    for schema in chosen:
        sa_schema = None if dialect_key == "sqlite" else schema
        tables = col.call(f"{schema}: tables", lambda: insp.get_table_names(schema=sa_schema), [])
        views = col.call(f"{schema}: views", lambda: insp.get_view_names(schema=sa_schema), [])
        matviews = col.call(f"{schema}: materialized views", lambda: insp.get_materialized_view_names(schema=sa_schema), [])

        kinds: dict[str, str] = {}
        for name in tables:
            kinds[name] = "table"
        for name in views:
            kinds[name] = "view"
        for name in matviews:
            kinds[name] = "materialized_view"
        if accessible_by_schema is not None:
            allowed = accessible_by_schema.get(schema, set())
            hidden = [n for n in kinds if n not in allowed]
            kinds = {n: k for n, k in kinds.items() if n in allowed}
            if hidden and schemas:
                out["warnings"].append(f"{schema}: {len(hidden)} object(s) hidden because this account cannot read them")
        if not kinds:
            out["skipped_schemas"][schema] = "no readable tables or views"
            if schemas:
                out["warnings"].append(f"requested schema {schema!r} has no tables or views this account can read")
            continue

        names_in_schema = list(kinds)
        columns = col.call(
            f"{schema}: columns",
            lambda: insp.get_multi_columns(schema=sa_schema, filter_names=names_in_schema, kind=ObjectKind.ANY),
            {},
        )
        pks = col.call(f"{schema}: primary keys", lambda: insp.get_multi_pk_constraint(schema=sa_schema, filter_names=names_in_schema), {})
        uniques = col.call(f"{schema}: unique constraints", lambda: insp.get_multi_unique_constraints(schema=sa_schema, filter_names=names_in_schema), {})
        checks = col.call(f"{schema}: check constraints", lambda: insp.get_multi_check_constraints(schema=sa_schema, filter_names=names_in_schema), {})
        fks = col.call(f"{schema}: foreign keys", lambda: insp.get_multi_foreign_keys(schema=sa_schema, filter_names=names_in_schema), {})
        indexes = col.call(f"{schema}: indexes", lambda: insp.get_multi_indexes(schema=sa_schema, filter_names=names_in_schema), {})
        comments = col.call(
            f"{schema}: table comments",
            lambda: insp.get_multi_table_comment(schema=sa_schema, filter_names=names_in_schema, kind=ObjectKind.ANY),
            {},
        )

        tables_out: dict[str, Any] = {}
        for name, kind in sorted(kinds.items()):
            key = (sa_schema, name)
            tables_out[name] = {
                "kind": kind,
                "comment": _clean((comments.get(key) or {}).get("text")),
                "columns": [_column(c, insp.dialect) for c in columns.get(key, [])],
                "primary_key": [str(c) for c in (pks.get(key) or {}).get("constrained_columns", [])],
                "unique_keys": [
                    {"name": u.get("name"), "columns": [str(c) for c in u.get("column_names", [])]} for u in uniques.get(key, [])
                ],
                "check_constraints": [{"name": c.get("name"), "sqltext": _clean(c.get("sqltext"))} for c in checks.get(key, [])],
                "foreign_keys": [
                    {
                        "name": f.get("name"),
                        "columns": [str(c) for c in f.get("constrained_columns", [])],
                        "referred_schema": f.get("referred_schema"),
                        "referred_table": f.get("referred_table"),
                        "referred_columns": [str(c) for c in f.get("referred_columns", [])],
                    }
                    for f in fks.get(key, [])
                ],
                "indexes": [
                    {"name": i.get("name"), "columns": [str(c) for c in i.get("column_names", []) if c], "unique": bool(i.get("unique"))}
                    for i in indexes.get(key, [])
                ],
            }
        out["schemas"][schema] = {"tables": tables_out}
    return out


_PG_ACCESSIBLE_SQL = text(
    """
    SELECT n.nspname AS schema_name, c.relname AS table_name
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'
      AND has_schema_privilege(n.nspname, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
    """
)
_ORACLE_USERS_SQL = text("SELECT username FROM all_users WHERE oracle_maintained = 'N'")


def reflect_database(sync_conn: Any, dialect_key: str, schemas: list[str] | None = None) -> dict[str, Any]:
    """Reflect every readable, non-system schema (or just ``schemas``) of the connected database."""
    insp = inspect(sync_conn)
    extra_warnings: list[str] = []

    accessible: set[tuple[str, str]] | None = None
    if dialect_key == "postgresql":
        try:
            accessible = {(r[0], r[1]) for r in sync_conn.execute(_PG_ACCESSIBLE_SQL)}
        except Exception as exc:  # noqa: BLE001
            extra_warnings.append(f"access check unavailable ({type(exc).__name__}); unreadable tables may be listed")

    candidates: list[str] | None = None
    if dialect_key == "oracle":
        try:
            candidates = [r[0] for r in sync_conn.execute(_ORACLE_USERS_SQL)]
        except Exception:  # noqa: BLE001 - pre-12c: fall back to the built-in list of maintained accounts
            candidates = None

    result = reflect_with_inspector(insp, dialect_key, schemas, accessible, candidates)
    result["warnings"].extend(extra_warnings)
    return result


__all__ = ["is_system_schema", "reflect_database", "reflect_with_inspector"]
