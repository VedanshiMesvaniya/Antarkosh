"""Phase 6 / step 6b: name handling (pure functions)."""

from __future__ import annotations

import pytest

from src.sql.relationships.model import Column, Table, type_family
from src.sql.relationships.names import (
    NameIndex,
    learn_common_columns,
    learn_table_prefixes,
    normalized_column_name,
    parse_id_column,
    singular,
    split_identifier,
    stem_tokens,
    table_stem,
)


def table(name: str, cols=("id",), schema=None, kind="table") -> Table:
    return Table(name, schema, f"{schema}.{name}" if schema else name, kind, tuple(Column(c, "INTEGER", type_family("INTEGER")) for c in cols))


@pytest.mark.parametrize(
    "name,tokens",
    [
        ("po_head", ["po", "head"]), ("PartyID", ["party", "id"]), ("POHead", ["po", "head"]), ("customerID", ["customer", "id"]),
        ("ItemMaster2", ["item", "master", "2"]), ("tbl-Customers", ["tbl", "customers"]), ("ORDER_ID", ["order", "id"]),
        ("  spaced name ", ["spaced", "name"]), ("", []), ("__", []), ("[dbo].[PO]", ["dbo", "po"]),
    ],
)
def test_identifiers_split_the_same_way_whatever_the_style(name, tokens):
    assert split_identifier(name) == tokens


@pytest.mark.parametrize(
    "word,one",
    [("customers", "customer"), ("addresses", "address"), ("categories", "category"), ("status", "status"), ("boxes", "box"),
     ("people", "person"), ("data", "data"), ("orders", "order"), ("lines", "line"), ("analysis", "analysis"), ("glass", "glass"),
     ("companies", "company"), ("id", "id"), ("bus", "bus"), ("statuses", "status"), ("PARTIES", "party"), ("branches", "branch")],
)
def test_singular(word, one):
    assert singular(word) == one


def test_only_the_head_noun_is_singularised():
    assert stem_tokens(["po", "details"]) == ("po", "detail") and stem_tokens([]) == ()


def test_table_prefixes_are_learned_from_the_names_not_configured():
    names = [f"tbl_{x}" for x in ("customers", "orders", "items", "parties", "stock", "price")] + ["audit_log", "users", "roles"]
    assert learn_table_prefixes(names) == frozenset({"tbl"})
    assert learn_table_prefixes(["tbl_a", "tbl_b", "tbl_c", "x_y", "x_z", "q_r"] * 1 + ["alpha_beta"] * 9) == frozenset()  # too few and too small a share
    assert learn_table_prefixes([f"customers_{i}" for i in range(10)]) == frozenset()  # longer than 4 letters
    assert learn_table_prefixes(["orders", "items", "parties"] * 4) == frozenset()  # single-word names have no prefix
    assert learn_table_prefixes([]) == frozenset()


def test_a_table_name_reduces_to_comparable_tokens():
    assert table_stem("tbl_customers", frozenset({"tbl"})) == ("customer",)
    assert table_stem("Customers") == ("customer",) and table_stem("PO_Details") == ("po", "detail")
    assert table_stem("tbl", frozenset({"tbl"})) == ("tbl",)  # stripping never empties a name
    assert table_stem("tbl_customers") == ("tbl", "customer")  # not learned, not stripped


@pytest.mark.parametrize(
    "name,stem,suffix,glued",
    [
        ("customer_id", ("customer",), "id", False), ("CustomerID", ("customer",), "id", False), ("po_no", ("po",), "no", False),
        ("invoice_code", ("invoice",), "code", False), ("id_customer", ("customer",), "id", False), ("fk_party", ("party",), "fk", False),
        ("id", (), "id", False), ("code", (), "code", False), ("custid", ("cust",), "id", True), ("partyid", ("party",), "id", True),
        ("orders_id", ("order",), "id", False), ("billing_address_id", ("billing", "address"), "id", False),
    ],
)
def test_id_like_columns_are_read_as_stem_plus_suffix(name, stem, suffix, glued):
    parsed = parse_id_column(name)
    assert parsed is not None and (parsed.stem, parsed.suffix, parsed.glued) == (stem, suffix, glued)


@pytest.mark.parametrize("name", ["paid", "valid", "name", "status", "created_at", "total", "idea", "id3x", "x_id", "7_id", "", "void", "rapid"])
def test_names_that_are_not_references_are_not_read_as_ids(name):
    assert parse_id_column(name) is None


def test_column_names_are_compared_without_regard_to_style():
    assert normalized_column_name("PartyID") == normalized_column_name("party_id") == "party_id"


def test_common_columns_are_learned_and_the_seed_list_is_always_included():
    tables = [table(f"t{i}", ("id", "company_id", f"own_{i}", "created_at")) for i in range(8)] + [table("v", ("only_in_view",), kind="view")]
    common = learn_common_columns(tables)
    assert {"company_id", "id", "created_at"} <= common and "own_0" not in common and "only_in_view" not in common
    assert "name" in common and "status" in common  # seed
    assert "company_id" not in learn_common_columns(tables[:3])  # below the minimum table count
    assert learn_common_columns([]) == frozenset(learn_common_columns([]))


def test_name_index_finds_tables_by_the_way_a_column_refers_to_them():
    tables = [table("customers"), table("tbl_customers_old"), table("erp_party"), table("party_group"), table("v_customers", kind="view"),
              table("orders", schema="sales"), table("Order_Items")]
    idx = NameIndex(tables, prefixes=frozenset({"tbl"}))
    assert [t.name for t in idx.exact(("customer",))] == ["customers"]  # the view is never offered
    assert [t.name for t in idx.exact(("order", "item"))] == ["Order_Items"]
    assert sorted(t.name for t in idx.by_head("party")) == ["erp_party"]
    assert sorted(t.name for t in idx.by_abbreviation("cust")) == ["customers", "tbl_customers_old"]
    assert [t.name for t in idx.by_abbreviation("par")] == ["party_group"]  # erp_party's first word is "party" but it is a prefixed name
    assert idx.by_abbreviation("cu") == []  # too short to be trusted
    assert idx.by_abbreviation("customer") == []  # a full word is an exact match, not an abbreviation
    assert idx.exact(("nothing",)) == [] and idx.by_head("nothing") == []


def test_name_index_learns_prefixes_by_itself_when_none_are_given():
    tables = [table(f"tbl_{n}") for n in ("customers", "orders", "items", "parties", "stock", "price")] + [table("roles")]
    idx = NameIndex(tables)
    assert idx.prefixes == frozenset({"tbl"}) and [t.name for t in idx.exact(("customer",))] == ["tbl_customers"]
