"""Phase 6 / step 6c: the detectors (steps 4 to 15), one rule at a time on synthetic schemas."""

from __future__ import annotations

import random

from src.sql.relationships.context import prepare, twin_key
from src.sql.relationships.detectors import (
    abbreviated_stem,
    code_like_columns,
    declared_fks,
    detect,
    header_detail,
    junction_tables,
    proc_edges,
    role_prefix,
    same_column_name,
    self_reference,
    table_id_columns,
)
from tests.sql.relationships_fixtures import build, ctx_of, proc_edge, table, triples

INT, TXT = "INTEGER", "VARCHAR(50)"


def customers_orders(**kw):
    return ctx_of(
        table("customers", {"id": INT, "name": TXT}, pk=("id",)),
        table("orders", {"id": INT, "customer_id": INT}, pk=("id",)),
        **kw,
    )


# ================================================================= step 4

def test_declared_foreign_keys_become_candidates_with_full_weight():
    ctx = ctx_of(
        table("customers", {"id": INT}, pk=("id",)), table("orders", {"id": INT, "cust": INT}, pk=("id",)),
        fks=(("orders", "cust", "customers", "id"),),
    )
    (c,) = declared_fks(ctx)
    assert c.key == ("orders", "cust", "customers", "id") and c.rule == "declared" and c.weight == 1.0 and not c.symmetric


# ================================================================= step 5

def _two_unrelated(**kw):
    return ctx_of(table("alpha", {"x": INT, "k": INT}), table("beta", {"y": INT, "k2": INT}), **kw)


def test_proc_edge_weight_follows_confidence():
    hi = proc_edges(_two_unrelated(proc=(proc_edge("alpha", "x", "beta", "y", "high"),)))
    lo = proc_edges(_two_unrelated(proc=(proc_edge("alpha", "x", "beta", "y", "low", routines=1),)))
    assert hi[0].weight == 0.90 and lo[0].weight == 0.55 and hi[0].rule == "proc"


def test_proc_edge_with_no_key_and_no_names_is_symmetric_and_stable():
    a = proc_edges(_two_unrelated(proc=(proc_edge("alpha", "x", "beta", "y"),)))
    b = proc_edges(_two_unrelated(proc=(proc_edge("beta", "y", "alpha", "x"),)))
    assert a[0].symmetric and a == b  # the same fact gives the same candidate whichever side was written first


def test_proc_edge_is_oriented_by_the_key():
    ctx = ctx_of(
        table("customers", {"id": INT}, pk=("id",)), table("orders", {"id": INT, "buyer": INT}, pk=("id",)),
        proc=(proc_edge("customers", "id", "orders", "buyer"),),
    )
    (c,) = proc_edges(ctx)
    assert c.key == ("orders", "buyer", "customers", "id") and not c.symmetric


def test_proc_edge_between_two_keys_is_oriented_by_the_column_name():
    ctx = ctx_of(
        table("customer", {"customer_id": INT}, pk=("customer_id",)),
        table("customer_profile", {"customer_id": INT}, pk=("customer_id",)),
        proc=(proc_edge("customer", "customer_id", "customer_profile", "customer_id"),),
    )
    (c,) = proc_edges(ctx)  # customer_id is named after `customer`, so customer_profile is the child
    assert c.key == ("customer_profile", "customer_id", "customer", "customer_id") and not c.symmetric


def test_proc_edge_between_two_keys_with_no_naming_hint_is_symmetric():
    ctx = ctx_of(table("alpha", {"id": INT}, pk=("id",)), table("beta", {"id": INT}, pk=("id",)), proc=(proc_edge("alpha", "id", "beta", "id"),))
    (c,) = proc_edges(ctx)
    assert c.symmetric


def test_proc_self_join_is_kept_and_a_column_joined_to_itself_is_not():
    ctx = ctx_of(
        table("employee", {"id": INT, "boss": INT}, pk=("id",)),
        proc=(proc_edge("employee", "id", "employee", "boss"), proc_edge("employee", "id", "employee", "id")),
    )
    assert triples(proc_edges(ctx)) == {("employee", "boss", "employee", "id")}


def test_proc_edges_on_views_are_ignored():
    ctx = ctx_of(
        table("customers", {"id": INT}, pk=("id",)), table("v_orders", {"customer_id": INT}, kind="view"),
        proc=(proc_edge("v_orders", "customer_id", "customers", "id"),),
    )
    assert proc_edges(ctx) == []


# ================================================================= step 6 (and 12, 13: prefixes, case, plural)

def test_table_id_snake_case_plural_table():
    assert triples(table_id_columns(customers_orders())) == {("orders", "customer_id", "customers", "id")}


def test_table_id_pascal_case():
    ctx = ctx_of(
        table("Customers", {"CustomerID": INT}, pk=("CustomerID",)),
        table("Orders", {"OrderID": INT, "CustomerID": INT}, pk=("OrderID",)),
    )
    assert triples(table_id_columns(ctx)) == {("Orders", "CustomerID", "Customers", "CustomerID")}


def test_table_id_with_learned_table_prefix():
    names = ["customers", "products", "invoices", "payments", "vendors", "orders"]
    tables = [table(f"tbl_{n}", {"id": INT, "customer_id": INT} if n == "orders" else {"id": INT}, pk=("id",)) for n in names]
    assert ("tbl_orders", "customer_id", "tbl_customers", "id") in triples(table_id_columns(ctx_of(*tables)))


def test_table_id_other_spellings_of_a_reference():
    ctx = ctx_of(
        table("party", {"id": INT}, pk=("id",)),
        table("a", {"id": INT, "fk_party": INT, "id_party": INT, "partyid": INT, "PartyId": INT}, pk=("id",)),
    )
    assert {c[1] for c in triples(table_id_columns(ctx))} == {"fk_party", "id_party", "partyid", "PartyId"}


def test_table_id_ignores_a_table_pointing_at_itself_and_names_that_are_not_references():
    ctx = ctx_of(
        table("customer", {"customer_id": INT, "paid": INT, "valid": INT, "x_id": INT, "id3x": INT}, pk=("customer_id",)),
    )
    assert table_id_columns(ctx) == []


def test_table_id_prefers_the_childs_own_schema():
    ctx = ctx_of(
        table("customer", {"id": INT}, pk=("id",), schema="sales"), table("customer", {"id": INT}, pk=("id",), schema="hr"),
        table("orders", {"id": INT, "customer_id": INT}, pk=("id",), schema="sales"),
    )
    assert triples(table_id_columns(ctx)) == {("sales.orders", "customer_id", "sales.customer", "id")}


def test_table_id_needs_a_parent_column_to_point_at():
    ctx = ctx_of(table("customer", {"name": TXT}), table("orders", {"customer_id": INT}))
    assert table_id_columns(ctx) == []  # no key, no `id`, no column of that name


def test_table_id_does_not_target_views():
    ctx = ctx_of(table("customer", {"id": INT}, pk=("id",), kind="view"), table("orders", {"customer_id": INT}))
    assert table_id_columns(ctx) == []


# ================================================================= step 7

def test_same_name_as_a_parent_key():
    ctx = ctx_of(
        table("customers", {"cust_no": TXT}, unique=(("cust_no",),)), table("orders", {"id": INT, "cust_no": TXT}, pk=("id",)),
    )
    (c,) = same_column_name(ctx)
    assert c.key == ("orders", "cust_no", "customers", "cust_no") and not c.symmetric


def test_same_name_never_joins_generic_columns():
    ctx = ctx_of(
        table("a", {"code": TXT, "status": TXT}, unique=(("code",), ("status",))), table("b", {"code": TXT, "status": TXT}),
    )
    assert same_column_name(ctx) == []


def test_same_name_never_joins_a_learned_generic_column():
    # `branch_ref` is in 6 of 12 tables, so it is learned as generic: a unique one in t0 proves nothing about the others
    tables = [table(f"t{i}", {"id": INT, "branch_ref": TXT}, pk=("id",), unique=(("branch_ref",),) if i == 0 else ()) for i in range(6)]
    tables += [table(f"u{i}", {"id": INT}, pk=("id",)) for i in range(6)]
    assert same_column_name(ctx_of(*tables)) == []
    # the same name in only 2 of 12 tables is not generic, so it is a candidate
    tables = [table(f"t{i}", {"id": INT, "branch_ref": TXT}, pk=("id",), unique=(("branch_ref",),) if i == 0 else ()) for i in range(2)]
    tables += [table(f"u{i}", {"id": INT}, pk=("id",)) for i in range(10)]
    assert triples(same_column_name(ctx_of(*tables))) == {("t1", "branch_ref", "t0", "branch_ref")}


def test_same_name_skips_a_name_that_is_a_key_in_many_tables():
    tables = [table(f"t{i}", {"slug": TXT}, unique=(("slug",),)) for i in range(7)] + [table("user_page", {"slug": TXT})]
    tables += [table(f"u{i}", {"id": INT}, pk=("id",)) for i in range(40)]  # keeps `slug` under the learned-generic share
    assert same_column_name(ctx_of(*tables)) == []


def test_same_name_between_two_keys_is_said_once_and_marked_symmetric():
    ctx = ctx_of(
        table("customer", {"customer_id": INT}, pk=("customer_id",)), table("customer_profile", {"customer_id": INT}, pk=("customer_id",)),
    )
    (c,) = same_column_name(ctx)
    assert c.symmetric and c.key == ("customer", "customer_id", "customer_profile", "customer_id")


def test_same_name_between_two_unique_non_id_columns_is_skipped():
    ctx = ctx_of(table("a", {"email": TXT}, unique=(("email",),)), table("b", {"email": TXT}, unique=(("email",),)))
    assert same_column_name(ctx) == []


# ================================================================= step 8

def test_abbreviation_matches_exactly_one_table():
    ctx = ctx_of(table("customer", {"id": INT}, pk=("id",)), table("orders", {"cust_id": INT}))
    assert triples(abbreviated_stem(ctx)) == {("orders", "cust_id", "customer", "id")}


def test_abbreviation_is_skipped_when_two_tables_fit():
    ctx = ctx_of(table("customer", {"id": INT}, pk=("id",)), table("custom", {"id": INT}, pk=("id",)), table("orders", {"cust_id": INT}))
    assert abbreviated_stem(ctx) == []


def test_abbreviation_of_a_multi_word_table_and_consonant_forms():
    ctx = ctx_of(table("po_header", {"id": INT}, pk=("id",)), table("po_detail", {"po_hdr_id": INT}))
    assert triples(abbreviated_stem(ctx)) == {("po_detail", "po_hdr_id", "po_header", "id")}


def test_abbreviation_yields_to_an_exact_match_and_ignores_two_letter_shorts():
    ctx = ctx_of(
        table("customer", {"id": INT}, pk=("id",)), table("cust", {"id": INT}, pk=("id",)),
        table("orders", {"cust_id": INT}), table("emp_x", {"id": INT}, pk=("id",)), table("t", {"em_id": INT}),
    )
    assert abbreviated_stem(ctx) == []  # `cust` exists as a table (rule 6 owns it); `em` is too short


# ================================================================= step 9

def test_role_prefix_points_at_the_table_after_the_role_word():
    ctx = ctx_of(
        table("address", {"id": INT}, pk=("id",)),
        table("orders", {"id": INT, "billing_address_id": INT, "shipping_address_id": INT}, pk=("id",)),
    )
    assert triples(role_prefix(ctx)) == {("orders", "billing_address_id", "address", "id"), ("orders", "shipping_address_id", "address", "id")}


def test_role_prefix_stays_quiet_when_the_whole_name_is_a_table():
    ctx = ctx_of(table("billing_address", {"id": INT}, pk=("id",)), table("address", {"id": INT}, pk=("id",)), table("o", {"billing_address_id": INT}))
    assert role_prefix(ctx) == []


# ================================================================= step 10

def test_self_reference_parent_id_and_parent_with_own_stem():
    ctx = ctx_of(
        table("category", {"id": INT, "parent_id": INT, "parent_category_id": INT, "parent_region_id": INT}, pk=("id",)),
    )
    got = {c.key: c.weight for c in self_reference(ctx)}
    assert got == {("category", "parent_id", "category", "id"): 0.55, ("category", "parent_category_id", "category", "id"): 0.55}


def test_self_reference_by_a_role_word_is_weaker():
    ctx = ctx_of(table("employee", {"id": INT, "manager_id": INT}, pk=("id",)))
    (c,) = self_reference(ctx)
    assert c.key == ("employee", "manager_id", "employee", "id") and c.weight == 0.40


def test_self_reference_needs_a_single_column_key_and_never_uses_the_key_itself():
    assert self_reference(ctx_of(table("t", {"a": INT, "b": INT, "parent_id": INT}, pk=("a", "b")))) == []
    assert self_reference(ctx_of(table("t", {"parent_id": INT}, pk=("parent_id",)))) == []


# ================================================================= step 11

def test_code_like_to_a_unique_column_of_the_same_stem():
    ctx = ctx_of(
        table("po", {"id": INT, "po_no": TXT}, pk=("id",), unique=(("po_no",),)), table("po_line", {"id": INT, "po_number": TXT}, pk=("id",)),
    )
    assert triples(code_like_columns(ctx)) == {("po_line", "po_number", "po", "po_no")}


def test_code_like_needs_a_unique_target_and_leaves_equal_names_to_rule_7():
    no_unique = ctx_of(table("po", {"id": INT, "po_no": TXT}, pk=("id",)), table("po_line", {"po_number": TXT}))
    assert code_like_columns(no_unique) == []  # native catalog path: no unique keys known
    same = ctx_of(table("po", {"po_no": TXT}, unique=(("po_no",),)), table("po_line", {"po_no": TXT}))
    assert code_like_columns(same) == [] and len(same_column_name(same)) == 1


# ================================================================= steps 14 and 15

def _enrollment():
    return ctx_of(
        table("student", {"id": INT}, pk=("id",)), table("course", {"id": INT}, pk=("id",)),
        table("enrollment", {"student_id": INT, "course_id": INT, "grade": TXT}, pk=("student_id", "course_id")),
    )


def test_junction_boosts_both_parts_of_a_composite_key():
    ctx = _enrollment()
    boosts = junction_tables(ctx, detect_base(ctx))
    assert triples(boosts) == {("enrollment", "student_id", "student", "id"), ("enrollment", "course_id", "course", "id")}
    assert {c.weight for c in boosts} == {0.20}


def test_junction_needs_two_references_in_the_key():
    ctx = ctx_of(table("student", {"id": INT}, pk=("id",)), table("note", {"student_id": INT, "n": INT}, pk=("student_id", "n")))
    assert junction_tables(ctx, detect_base(ctx)) == []


def test_header_detail_is_a_parent_key_plus_a_line_number():
    ctx = ctx_of(
        table("orders", {"id": INT}, pk=("id",)), table("order_line", {"order_id": INT, "line_no": INT}, pk=("order_id", "line_no")),
    )
    assert triples(header_detail(ctx, detect_base(ctx))) == {("order_line", "order_id", "orders", "id")}


def test_header_detail_refuses_a_key_part_that_is_not_a_line_number():
    ctx = ctx_of(
        table("orders", {"id": INT}, pk=("id",)), table("order_x", {"order_id": INT, "region": TXT}, pk=("order_id", "region")),
    )
    assert header_detail(ctx, detect_base(ctx)) == []


def detect_base(ctx):
    return [c for c in detect(ctx) if c.rule not in ("junction", "header_detail")]


# ================================================================= twin key helper

def test_twin_key_removes_copy_markers_digits_and_versions():
    assert twin_key(("product",)) == (("product",), False)
    assert twin_key(("product", "bak")) == (("product",), True)
    assert twin_key(("product", "2023")) == (("product",), True)
    assert twin_key(("product", "v", "2")) == (("product",), True)
    assert twin_key(("test",)) == (("test",), False)  # a table that is only a marker is left alone


# ================================================================= whole run

def test_views_are_never_a_side_of_a_candidate():
    ctx = ctx_of(
        table("customers", {"id": INT}, pk=("id",)), table("v_orders", {"customer_id": INT}, kind="view"),
        table("orders", {"customer_id": INT}),
    )
    assert all("v_orders" not in (c.child, c.parent) for c in detect(ctx))


def test_detection_is_deterministic_whatever_order_the_catalog_lists_tables_in():
    tables = [
        table("customers", {"id": INT, "name": TXT}, pk=("id",)), table("products", {"id": INT}, pk=("id",)),
        table("orders", {"id": INT, "customer_id": INT, "billing_customer_id": INT}, pk=("id",)),
        table("order_items", {"order_id": INT, "product_id": INT, "line_no": INT}, pk=("order_id", "line_no")),
        table("employee", {"id": INT, "manager_id": INT}, pk=("id",)),
    ]
    first = detect(prepare(build(*tables)))
    for seed in range(5):
        shuffled = tables[:]
        random.Random(seed).shuffle(shuffled)
        assert detect(prepare(build(*shuffled))) == first
    assert first == sorted(first, key=lambda c: (c.child, c.child_column, c.parent, c.parent_column, c.rule, c.weight, c.symmetric))
