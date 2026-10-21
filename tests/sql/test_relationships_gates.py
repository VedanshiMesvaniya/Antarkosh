"""Phase 6 / step 6d: gates, boosts, direction, cardinality and ambiguity (steps 16 to 22)."""

from __future__ import annotations

import random

from src.sql.relationships.candidates import WEIGHTS, Candidate, Edge, noisy_or
from src.sql.relationships.context import prepare
from src.sql.relationships.detectors import detect
from src.sql.relationships.gates import MAX_PARENTS_PER_COLUMN, resolve
from tests.sql.relationships_fixtures import build, ctx_of, proc_edge, table

INT, TXT = "INTEGER", "VARCHAR(50)"


def run(*tables, **kw):
    ctx = prepare(build(*tables, **kw))
    edges, report = resolve(ctx, detect(ctx))
    return {e.key: e for e in edges}, report


def cand(child, ccol, parent, pcol, rule="table_id", weight=0.6, symmetric=False):
    return Candidate(child, ccol, parent, pcol, rule, weight, symmetric)


def resolve_of(ctx, *cands):
    edges, report = resolve(ctx, list(cands))
    return {e.key: e for e in edges}, report


# ================================================================= scoring

def test_noisy_or_counts_name_rules_as_one_witness():
    assert noisy_or({"table_id": 0.60, "same_name": 0.55}) == 0.60  # same names read twice: still one witness
    assert noisy_or({"table_id": 0.60, "proc": 0.55}) == 0.82
    assert noisy_or({"declared": 1.0, "table_id": 0.6}) == 1.0
    assert noisy_or({}) == 0.0


def test_a_name_match_alone_never_reaches_the_high_threshold():
    best_name_only = {r: w for r, w in WEIGHTS.items() if r in ("table_id", "same_name", "abbrev", "role_prefix", "self_ref", "code_like")}
    assert noisy_or(best_name_only) < 0.70
    assert noisy_or({**best_name_only, "indexed": 0.05, "junction": 0.20}) < 0.70  # a name + small structure hints still needs more


# ================================================================= 16 type gate

def test_type_gate_drops_a_reference_between_incompatible_types():
    edges, report = run(table("customers", {"id": INT}, pk=("id",)), table("orders", {"customer_id": TXT}))
    assert edges == {} and report.dropped["type"] == 1


def test_type_gate_allows_integer_widths_and_oracle_number():
    edges, _ = run(table("customers", {"id": "NUMBER(10)"}, pk=("id",)), table("orders", {"customer_id": "BIGINT"}))
    assert ("orders", "customer_id", "customers", "id") in edges


def test_type_gate_never_drops_a_declared_foreign_key():
    edges, report = run(
        table("customers", {"id": INT}, pk=("id",)), table("orders", {"cust": TXT}), fks=(("orders", "cust", "customers", "id"),),
    )
    assert ("orders", "cust", "customers", "id") in edges and report.dropped["type"] == 0


# ================================================================= 17 parent-key gate

def test_parent_key_gate_drops_a_target_that_is_not_a_key():
    edges, report = run(table("customer", {"name": TXT, "id": INT}), table("orders", {"customer_id": INT}))
    assert edges == {} and report.dropped["parent_key"] == 1


def test_parent_key_gate_is_waived_by_procedure_evidence():
    edges, _ = run(
        table("customer", {"name": TXT, "id": INT}), table("orders", {"customer_id": INT}),
        proc=(proc_edge("orders", "customer_id", "customer", "id"),),
    )
    e = edges[("orders", "customer_id", "customer", "id")]
    assert {"proc", "table_id"} <= set(e.evidence) and e.score >= 0.70


def test_with_only_primary_keys_known_the_name_rules_still_work():
    # the native catalog records no unique keys and no indexes; rules that need them just find nothing
    edges, _ = run(table("customers", {"id": INT}, pk=("id",)), table("orders", {"customer_id": INT}))
    assert list(edges) == [("orders", "customer_id", "customers", "id")]


# ================================================================= 18, 19 boosts

def test_indexed_child_column_adds_a_small_boost():
    edges, _ = run(table("customers", {"id": INT}, pk=("id",)), table("orders", {"customer_id": INT}, indexes=(("customer_id",),)))
    e = edges[("orders", "customer_id", "customers", "id")]
    assert e.evidence["indexed"] == 0.05 and e.score == noisy_or({"table_id": 0.6, "indexed": 0.05})


def test_a_comment_naming_the_parent_adds_a_boost_but_an_unrelated_one_does_not():
    edges, _ = run(
        table("customers", {"id": INT}, pk=("id",)),
        table("orders", {"customer_id": (INT, "Reference to the customer who placed it"), "buyer_id": INT}),
        table("invoices", {"customer_id": (INT, "who pays")}),
        table("shipments", {"customer_id": INT}, comment="Goods sent to a customer"),
    )
    assert "comment" in edges[("orders", "customer_id", "customers", "id")].evidence
    assert "comment" not in edges[("invoices", "customer_id", "customers", "id")].evidence
    assert "comment" in edges[("shipments", "customer_id", "customers", "id")].evidence  # table comment counts too


# ================================================================= 20, 21 direction and cardinality

def test_shared_primary_key_edge_points_from_the_extension_table_to_the_base_table():
    edges, report = run(
        table("customer", {"customer_id": INT}, pk=("customer_id",)), table("customer_profile", {"customer_id": INT, "bio": TXT}, pk=("customer_id",)),
    )
    e = edges[("customer_profile", "customer_id", "customer", "customer_id")]
    assert e.cardinality == "shared_key" and e.direction_known and len(edges) == 1
    assert {"table_id", "same_name"} <= set(e.evidence) and e.score == noisy_or({"table_id": 0.6, "indexed": 0.05})  # names count once
    assert report.direction_unknown == 0


def test_a_unique_child_column_is_one_to_one_and_an_ordinary_one_is_many_to_one():
    edges, _ = run(
        table("customers", {"id": INT}, pk=("id",)), table("accounts", {"id": INT, "customer_id": INT}, pk=("id",), unique=(("customer_id",),)),
        table("orders", {"id": INT, "customer_id": INT}, pk=("id",)),
    )
    assert edges[("accounts", "customer_id", "customers", "id")].cardinality == "one_to_one"
    assert edges[("orders", "customer_id", "customers", "id")].cardinality == "many_to_one"


def test_direction_open_edge_is_decided_by_row_counts():
    tables = [table("alpha", {"x": INT}, rows=10), table("beta", {"y": INT}, rows=1000)]
    p = (proc_edge("alpha", "x", "beta", "y"),)
    edges, _ = run(*tables, proc=p)
    assert list(edges) == [("beta", "y", "alpha", "x")]  # the table with more rows is the "many" side
    assert edges[("beta", "y", "alpha", "x")].direction_known


def test_direction_stays_unknown_without_names_or_row_counts_and_is_flagged():
    for rows in ((None, None), (-1, -1), (50, 50), (0, 100)):  # missing, never analysed, equal, one unknown
        edges, report = run(table("alpha", {"x": INT}, rows=rows[0]), table("beta", {"y": INT}, rows=rows[1]), proc=(proc_edge("alpha", "x", "beta", "y"),))
        (e,) = edges.values()
        assert not e.direction_known and report.direction_unknown == 1, rows


def test_opposite_directions_for_one_column_pair_resolve_to_the_better_supported_one():
    ctx = ctx_of(table("a", {"id": INT, "x": INT}, pk=("id",), unique=(("x",),)), table("b", {"id": INT, "y": INT}, pk=("id",), unique=(("y",),)))
    edges, report = resolve_of(ctx, cand("a", "x", "b", "y", weight=0.6), cand("b", "y", "a", "x", "proc", 0.55))
    assert list(edges) == [("a", "x", "b", "y")] and report.dropped["direction_conflict"] == 1


def test_opposite_directions_with_equal_support_are_both_dropped_never_guessed():
    ctx = ctx_of(table("a", {"x": INT}, unique=(("x",),)), table("b", {"y": INT}, unique=(("y",),)))
    edges, report = resolve_of(ctx, cand("a", "x", "b", "y", "proc", 0.55), cand("b", "y", "a", "x", "proc", 0.55))
    assert edges == {} and report.dropped["direction_conflict"] == 2


def test_an_edge_with_unknown_direction_folds_into_the_known_one():
    ctx = ctx_of(table("a", {"id": INT, "x": INT}, pk=("id",), unique=(("x",),)), table("b", {"id": INT, "y": INT}, pk=("id",), unique=(("y",),)))
    edges, _ = resolve_of(ctx, cand("a", "x", "b", "y", "proc", 0.55, symmetric=True), cand("b", "y", "a", "x", "table_id", 0.6))
    assert list(edges) == [("b", "y", "a", "x")] and {"proc", "table_id"} <= set(edges[("b", "y", "a", "x")].evidence)


# ================================================================= 22 ambiguity

def test_an_index_does_not_rescue_a_generic_name_but_a_comment_does():
    ctx = ctx_of(
        table("a", {"code": TXT}, unique=(("code",),)), table("b", {"code": (TXT, "the a code")}, indexes=(("code",),)),
    )
    edges, _ = resolve_of(ctx, cand("b", "code", "a", "code", "same_name", 0.55))
    assert list(edges) == [("b", "code", "a", "code")]  # the comment names the parent: real corroboration


def test_generic_column_names_never_join_on_the_name_alone():
    ctx = ctx_of(table("a", {"code": TXT}, unique=(("code",),)), table("b", {"code": TXT}, unique=(("code",),)))
    edges, report = resolve_of(ctx, cand("b", "code", "a", "code", "same_name", 0.55))
    assert edges == {} and report.dropped["generic_name"] == 1


def test_twin_table_with_more_rows_wins():
    edges, report = run(
        table("product", {"id": INT}, pk=("id",), rows=3), table("products", {"id": INT}, pk=("id",), rows=5000), table("order_line", {"product_id": INT}),
    )
    assert list(edges) == [("order_line", "product_id", "products", "id")] and report.dropped["twin"] == 1


def test_backup_copy_loses_to_the_table_without_a_marker():
    edges, _ = run(table("product", {"id": INT}, pk=("id",)), table("product_bak", {"id": INT}, pk=("id",)), table("order_line", {"product_id": INT}))
    assert list(edges) == [("order_line", "product_id", "product", "id")]


def test_twins_nothing_can_tell_apart_are_both_kept_and_marked_ambiguous():
    edges, report = run(table("product", {"id": INT}, pk=("id",)), table("products", {"id": INT}, pk=("id",)), table("order_line", {"product_id": INT}))
    assert len(edges) == 2 and all(e.ambiguous for e in edges.values()) and report.ambiguous == 2


def test_twin_with_procedure_evidence_wins_over_one_without():
    edges, _ = run(
        table("product", {"id": INT}, pk=("id",), rows=100), table("products", {"id": INT}, pk=("id",), rows=100), table("order_line", {"product_id": INT}),
        proc=(proc_edge("order_line", "product_id", "product", "id"),),
    )
    assert list(edges) == [("order_line", "product_id", "product", "id")]


def test_a_declared_foreign_key_to_a_twin_is_never_dropped():
    edges, _ = run(
        table("product", {"id": INT}, pk=("id",), rows=100000), table("product_old", {"id": INT}, pk=("id",)), table("order_line", {"product_id": INT}),
        fks=(("order_line", "product_id", "product_old", "id"),),
    )
    assert ("order_line", "product_id", "product_old", "id") in edges


def test_at_most_three_parents_per_column_and_the_rest_are_counted():
    names = [f"t{i}" for i in range(5)]
    tables = [table(n, {"sku": TXT}, unique=(("sku",),)) for n in names] + [table("order_line", {"sku": TXT})]
    tables += [table(f"u{i}", {"id": INT}, pk=("id",)) for i in range(20)]
    edges, report = run(*tables)
    mine = [e for e in edges.values() if e.child == "order_line"]
    assert len(mine) == MAX_PARENTS_PER_COLUMN and all(e.ambiguous for e in mine) and report.dropped["over_cap"] == 2


def test_hard_evidence_edges_are_not_counted_against_the_cap():
    ctx = ctx_of(*[table(f"p{i}", {"id": INT}, pk=("id",)) for i in range(5)], table("c", {"x": INT}))
    proc = [cand("c", "x", f"p{i}", "id", "proc", 0.9) for i in range(5)]
    edges, report = resolve_of(ctx, *proc)
    assert len(edges) == 5 and report.dropped["over_cap"] == 0


# ================================================================= whole run

SHOP = (
    table("customers", {"id": INT, "name": TXT, "email": TXT}, pk=("id",), unique=(("email",),)),
    table("addresses", {"id": INT, "customer_id": INT}, pk=("id",)),
    table("products", {"id": INT, "category_id": INT, "name": TXT}, pk=("id",)),
    table("categories", {"id": INT, "parent_id": INT, "name": TXT}, pk=("id",)),
    table("orders", {"id": INT, "customer_id": INT, "billing_address_id": INT, "shipping_address_id": INT, "status": TXT}, pk=("id",)),
    table("order_items", {"order_id": INT, "line_no": INT, "product_id": INT, "qty": INT}, pk=("order_id", "line_no")),
    table("tags", {"id": INT}, pk=("id",)),
    table("product_tags", {"product_id": INT, "tag_id": INT}, pk=("product_id", "tag_id")),
    table("v_sales", {"customer_id": INT}, kind="view"),
)
SHOP_TRUTH = {
    ("addresses", "customer_id", "customers", "id"), ("products", "category_id", "categories", "id"),
    ("categories", "parent_id", "categories", "id"), ("orders", "customer_id", "customers", "id"),
    ("orders", "billing_address_id", "addresses", "id"), ("orders", "shipping_address_id", "addresses", "id"),
    ("order_items", "product_id", "products", "id"), ("product_tags", "product_id", "products", "id"), ("product_tags", "tag_id", "tags", "id"),
}


def test_a_realistic_schema_recovers_every_reference_and_invents_none():
    edges, report = run(*SHOP)
    got = set(edges)
    # order_items.order_id -> orders.id is the one reference no name rule is given here: its table is `orders` and the
    # column `order_id` is the singular of it, so it IS found; assert it separately to keep the ground truth explicit
    truth = SHOP_TRUTH | {("order_items", "order_id", "orders", "id")}
    assert got == truth, {"missing": truth - got, "extra": got - truth}
    assert report.direction_unknown == 0 and report.ambiguous == 0
    assert edges[("product_tags", "tag_id", "tags", "id")].evidence["junction"] == 0.20
    assert edges[("order_items", "order_id", "orders", "id")].evidence["header_detail"] == 0.20
    assert all(e.score < 0.70 for e in edges.values())  # nothing is "high" on names and structure alone


def test_resolve_is_deterministic_and_does_not_depend_on_table_order():
    first_edges, first_report = run(*SHOP)
    for seed in range(5):
        tables = list(SHOP)
        random.Random(seed).shuffle(tables)
        edges, report = run(*tables)
        assert list(edges) == list(first_edges) and report.as_counts() == first_report.as_counts()
        assert {k: (e.evidence, e.cardinality, e.direction_known, e.ambiguous) for k, e in edges.items()} == {
            k: (e.evidence, e.cardinality, e.direction_known, e.ambiguous) for k, e in first_edges.items()
        }


def test_report_counts_add_up():
    _, report = run(table("customers", {"id": INT}, pk=("id",)), table("orders", {"customer_id": TXT, "x": INT}))
    counts = report.as_counts()
    assert counts["edges_in"] == 1 and counts["edges_out"] == 0 and counts["dropped_type"] == 1


def test_edge_merge_keeps_the_best_weight_per_rule():
    a = Edge("c", "x", "p", "id", {"table_id": 0.6}, symmetric_only=True)
    b = Edge("c", "x", "p", "id", {"table_id": 0.5, "proc": 0.9}, symmetric_only=False)
    a.merge(b)
    assert a.evidence == {"table_id": 0.6, "proc": 0.9} and not a.symmetric_only
