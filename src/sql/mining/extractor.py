"""Facts from parsed statements (auto-schema Phase 5, step 2): which tables a routine reads and writes,
which columns it joins, and which string values it compares or assigns.

Only FACTS leave this module: table names, column names, short string constants. No statement text.

Rules that keep the facts honest:
* aliases are resolved (``UPDATE po .. FROM po_head po`` writes ``po_head``); ``#temp`` tables, ``@table``
  variables, CTE names and derived tables are never reported as tables;
* a join is an equality between two columns of different aliases in an ``ON`` / ``WHERE`` / ``HAVING``
  predicate, never a ``SET a = b`` assignment;
* a column with no alias is resolved only when exactly one table of the statement can own it;
* ``SELECT .. INTO x`` is a table only in T-SQL, elsewhere it is a variable;
* with a catalog, a table or column that does not exist in it is dropped and counted.

Origin: new (no previous file)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from src.sql.mining.catalog_index import CatalogIndex

MAX_VALUE_CHARS = 40


@dataclass
class RoutineFacts:
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    joins: set[tuple[tuple[str, str], tuple[str, str]]] = field(default_factory=set)  # ((table, column), (table, column)) sorted
    join_counts: dict[tuple[tuple[str, str], tuple[str, str]], int] = field(default_factory=dict)  # statements per edge
    values: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)  # (table, column) -> {value: statements}
    unknown_tables: int = 0  # tables not in the catalog (dropped)


def _is_temp(tb: exp.Table) -> bool:
    this = tb.this
    return isinstance(this, exp.Parameter) or bool(getattr(this, "args", {}).get("temporary")) or tb.name.startswith(("#", "@"))


def _raw_name(tb: exp.Table) -> str:
    return (f"{tb.db}.{tb.name}" if tb.db else tb.name).lower()


def _predicates(tree: exp.Expression):
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            yield on
    for merge in tree.find_all(exp.Merge):
        on = merge.args.get("on")
        if on is not None:
            yield on
    yield from tree.find_all(exp.Where, exp.Having)


def extract_routine_facts(trees: list[exp.Expression], engine: str, catalog: CatalogIndex | None = None) -> RoutineFacts:
    """Facts from every statement of one routine."""
    facts = RoutineFacts()
    tsql = engine == "mssql"
    temp_names: set[str] = set()
    cte_names: set[str] = set()

    for tree in trees:
        if not tsql:  # SELECT .. INTO <variable>
            for sel in tree.find_all(exp.Select):
                if sel.args.get("into") is not None:
                    sel.set("into", None)
        for cte in tree.find_all(exp.CTE):
            cte_names.add(cte.alias_or_name.lower())
        for tb in tree.find_all(exp.Table):
            if _is_temp(tb):
                temp_names.add(tb.name.lower())
        if isinstance(tree, exp.Create):
            tb = tree.find(exp.Table)
            if tb is not None and (_is_temp(tb) or tree.find(exp.TemporaryProperty) is not None):
                temp_names.add(tb.name.lower())

    seen: dict[int, str | None] = {}

    def canonical(tb: exp.Table) -> str | None:
        if id(tb) in seen:
            return seen[id(tb)]
        raw = _raw_name(tb)
        resolved: str | None
        if not raw or tb.name.lower() in cte_names or tb.name.lower() in temp_names:
            resolved = None
        elif catalog is None:
            resolved = raw
        else:
            resolved = catalog.resolve_table(raw)
            if resolved is None:
                facts.unknown_tables += 1  # counted once per reference
        seen[id(tb)] = resolved
        return resolved

    for tree in trees:
        skip = {id(x) for x in (tree.args.get("tables") or [])} if isinstance(tree, exp.Delete) else set()
        alias_map: dict[str, str] = {}  # alias or bare name -> canonical table
        statement_tables: list[str] = []
        for tb in tree.find_all(exp.Table):
            if id(tb) in skip:
                continue
            canon = canonical(tb)
            if canon is None:
                continue
            if canon not in statement_tables:
                statement_tables.append(canon)
            if tb.alias:
                alias_map[tb.alias.lower()] = canon
        for tb in tree.find_all(exp.Table):
            canon = canonical(tb) if id(tb) not in skip else None
            if canon is not None and not tb.alias:
                alias_map.setdefault(tb.name.lower(), canon)

        target: exp.Table | None = None
        if isinstance(tree, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
            this = tree.this
            if isinstance(this, exp.Table):
                target = this
            elif isinstance(this, exp.Expression):
                target = this.find(exp.Table)
        select_into = None
        if tsql:
            for sel in tree.find_all(exp.Select):
                into = sel.args.get("into")
                if isinstance(into, exp.Expression) and isinstance(getattr(into, "this", None), exp.Table):
                    select_into = into.this
        for tb in tree.find_all(exp.Table):
            if id(tb) in skip:
                continue
            canon = canonical(tb)
            if canon is None:
                continue
            if tb is target:
                # ``UPDATE <alias> .. FROM real_table <alias>``: the alias names a table from the FROM clause.
                if not tb.db and tb.name.lower() in alias_map and alias_map[tb.name.lower()] != canon:
                    facts.writes.add(alias_map[tb.name.lower()])
                else:
                    facts.writes.add(canon)
            elif tb is select_into:
                facts.writes.add(canon)
            else:
                facts.reads.add(canon)

        def resolve(
            col: exp.Column,
            alias_map: dict[str, str] = alias_map,
            statement_tables: list[str] = statement_tables,
        ) -> tuple[str, str, str | None] | None:
            """(table, column, alias used or None). An unqualified column needs a catalog, or a one-table statement."""
            name = col.name.lower()
            if col.table:
                table = alias_map.get(col.table.lower())
                if table is None or (catalog is not None and not catalog.has_column(table, name)):
                    return None
                return table, name, col.table.lower()
            if catalog is not None:
                owners = catalog.owners(statement_tables, name)
                return (owners[0], name, None) if len(owners) == 1 else None
            return (statement_tables[0], name, None) if len(statement_tables) == 1 else None

        for predicate in _predicates(tree):
            for eq in predicate.find_all(exp.EQ):
                if not (isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)):
                    continue
                a, b = resolve(eq.left), resolve(eq.right)
                if not a or not b:
                    continue
                if catalog is None and (a[2] is None or b[2] is None):
                    continue  # without a catalog an unqualified name may be a variable or parameter, not a column
                if (a[0], a[1]) == (b[0], b[1]):
                    continue
                if a[0] == b[0] and (a[2] is None or b[2] is None or a[2] == b[2]):
                    continue  # one table: only a self-join through two different aliases counts
                edge = tuple(sorted(((a[0], a[1]), (b[0], b[1]))))
                facts.joins.add(edge)  # type: ignore[arg-type]
                facts.join_counts[edge] = facts.join_counts.get(edge, 0) + 1  # type: ignore[index]

        def note(col: object, value: str) -> None:
            if not isinstance(col, exp.Column) or not value or len(value) > MAX_VALUE_CHARS or "\n" in value:
                return
            ref = resolve(col)
            if ref:
                per = facts.values.setdefault((ref[0], ref[1]), {})
                per[value] = per.get(value, 0) + 1

        for eq in tree.find_all(exp.EQ):
            for col, lit in ((eq.left, eq.right), (eq.right, eq.left)):
                if isinstance(lit, exp.Literal) and lit.is_string:
                    note(col, lit.this)
        for node in tree.find_all(exp.In):
            for item in node.expressions:
                if isinstance(item, exp.Literal) and item.is_string:
                    note(node.this, item.this)
        for case in tree.find_all(exp.Case):
            for branch in case.args.get("ifs") or []:
                if isinstance(branch.this, exp.Literal) and branch.this.is_string:
                    note(case.this, branch.this.this)

    return facts


__all__ = ["MAX_VALUE_CHARS", "RoutineFacts", "extract_routine_facts"]
