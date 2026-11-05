"""Phase 9d: routing hints and the synonym glossary are generated at sync time for any database."""

# ruff: noqa: F811

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.sql.relationships.model import graph_from_documents, load_graph
from src.sql.sa import hints_phase
from src.sql.semantics import routing
from src.sql.semantics.lexicon import SYNONYM_GROUPS
from src.sql.semantics.routing import build_hints, merge_routing_hints, merge_synonym_glossary
from tests.sql.relationships_fixtures import documents, table
from tests.sql.test_catalog_extraction import dbs, sqlite_db  # noqa: F401  (shared fixtures)
from tests.sql.test_resync_e2e import _flags, _sql, _sync
from tests.sql.test_resync_e2e import shop as shop_db  # noqa: F401  (shared fixture)

INT, TXT = "INTEGER", "VARCHAR(50)"
DB = "pg_live"


def graph_of(*tables: dict[str, Any], **kw: Any):
    schema_doc, inventory, _ = documents(*tables, **kw)
    g = graph_from_documents(schema_doc, inventory)
    return g, set(g.tables)


def shop() -> tuple[dict[str, Any], ...]:
    return (
        table("vendors", {"vendor_id": INT, "vendor_name": TXT}, pk=("vendor_id",)),
        table("purchase_orders", {"po_no": TXT, "vendor_id": INT, "total": INT}, pk=("po_no",)),
        table("customers", {"customer_id": INT, "email": TXT}, pk=("customer_id",)),
    )


def put(dbs: Path, *tables: dict[str, Any], profiles: dict[str, Any] | None = None, **kw: Any) -> None:
    schema_doc, inventory, _ = documents(*(tables or shop()), **kw)
    folder = dbs / DB / "schema"
    (folder / "schema.json").write_text(json.dumps(schema_doc), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    if profiles is None:
        (folder / "table_profiles.json").unlink(missing_ok=True)
    else:
        (folder / "table_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")


def read(dbs: Path, rel: str) -> Any:
    return json.loads((dbs / DB / rel).read_text(encoding="utf-8"))


# ================================================================= the lexicon


def test_the_lexicon_is_clean_single_lower_case_words_each_in_one_group():
    words = [w for g in SYNONYM_GROUPS for w in g]
    assert len(words) == len(set(words)) and all(w.isalpha() and w.islower() for w in words)
    assert all(len(g) >= 2 for g in SYNONYM_GROUPS)


# ================================================================= building (pure)


def test_a_group_is_used_only_for_words_the_schema_names_contain_and_keywords_are_the_other_words():
    g, eligible = graph_of(*shop())
    h = build_hints(g, eligible)
    assert set(h.glossary) == {"vendor", "customer"}
    assert "supplier" in h.glossary["vendor"] and "vendor" not in h.glossary["vendor"] and "client" in h.glossary["customer"]
    rule = next(r for r in h.anchor_rules if "supplier" in r["keywords"])
    assert rule["tables"] == ["vendors"]
    assert "vendor" not in rule["keywords"] and rule["auto"] == routing.GENERATED_BY
    assert all(len(k) >= routing.MIN_KEYWORD_CHARS and k.isalpha() and k.islower() for r in h.anchor_rules + h.fallback_rules for k in r["keywords"])


def test_a_group_the_schema_itself_tells_apart_is_skipped():
    g, eligible = graph_of(table("customers", {"customer_id": INT}), table("clients", {"client_id": INT}))
    h = build_hints(g, eligible)
    assert "customer" not in h.glossary and "client" not in h.glossary and h.stats["groups_ambiguous"] == 1
    assert h.fallback_rules == [] and h.anchor_rules == []


def test_a_concept_found_only_in_columns_gets_a_glossary_group_but_no_routing_rule():
    g, eligible = graph_of(table("orders", {"order_id": INT, "vendor_id": INT}))
    h = build_hints(g, eligible)
    assert "vendor" in h.glossary and h.fallback_rules == [] and h.anchor_rules == [] and h.stats["no_table_named"] == 1


def test_many_matching_tables_make_a_fallback_rule_only_and_too_many_make_none():
    g, eligible = graph_of(*(table(f"vendor_{c}", {"id": INT}) for c in "abcdef"))
    h = build_hints(g, eligible)
    assert len(h.fallback_rules) == 1 and h.anchor_rules == [] and len(h.fallback_rules[0]["tables"]) == 6
    g, eligible = graph_of(*(table(f"vendor_{c}", {"id": INT}) for c in "abcdefghi"))
    h = build_hints(g, eligible)
    assert h.fallback_rules == [] and h.stats["too_broad"] == 1 and "vendor" in h.glossary


def test_tables_the_retrieval_code_cannot_look_up_never_appear_in_a_rule():
    g, eligible = graph_of(table("Vendor Master", {"id": INT}), table("vendor$x", {"id": INT}), table("VENDORS", {"id": INT}))
    h = build_hints(g, eligible)
    assert h.anchor_rules[0]["tables"] == ["vendors"]  # bare and lower case, the way the retrieval code looks tables up


def test_only_eligible_tables_count_so_copies_and_test_tables_are_left_out():
    g, _ = graph_of(table("vendors", {"id": INT}), table("vendors_bak", {"id": INT}))
    only_real = {q for q in g.tables if q != "vendors_bak"}
    assert build_hints(g, only_real).anchor_rules[0]["tables"] == ["vendors"]


def test_building_is_deterministic_and_changes_nothing():
    g, eligible = graph_of(*shop())
    assert build_hints(g, eligible) == build_hints(g, eligible)


# ================================================================= the glossary file


def test_an_empty_template_seed_is_filled_and_a_second_run_changes_nothing(dbs: Path):
    g, eligible = graph_of(*shop())
    groups = build_hints(g, eligible).glossary
    assert read(dbs, "semantics/glossary.json") == {}
    out = merge_synonym_glossary(DB, groups, dbs)
    assert out["written"] and out["groups"] == 2 and set(read(dbs, "semantics/glossary.json")) == {"vendor", "customer"}
    assert read(dbs, routing.STATE_REL)["glossary"] == groups
    again = merge_synonym_glossary(DB, groups, dbs)
    assert again["unchanged"] and not again["written"]


def test_a_persons_groups_are_never_touched_and_win_a_collision(dbs: Path):
    path = dbs / DB / "semantics" / "glossary.json"
    path.write_text(json.dumps({"vendor": ["our", "own"], "lead": ["prospect"]}), encoding="utf-8")
    merge_synonym_glossary(DB, {"vendor": ["supplier"], "customer": ["client"]}, dbs)
    assert json.loads(path.read_text()) == {"vendor": ["our", "own"], "lead": ["prospect"], "customer": ["client"]}
    merge_synonym_glossary(DB, {}, dbs)  # switched off: only the generated group goes
    assert json.loads(path.read_text()) == {"vendor": ["our", "own"], "lead": ["prospect"]}


def test_a_group_a_person_edited_becomes_theirs_and_is_not_overwritten(dbs: Path):
    path = dbs / DB / "semantics" / "glossary.json"
    merge_synonym_glossary(DB, {"customer": ["client", "buyer"]}, dbs)
    path.write_text(json.dumps({"customer": ["client", "buyer", "my own word"]}), encoding="utf-8")
    merge_synonym_glossary(DB, {"customer": ["client", "buyer"]}, dbs)
    assert json.loads(path.read_text()) == {"customer": ["client", "buyer", "my own word"]}
    merge_synonym_glossary(DB, {}, dbs)
    assert json.loads(path.read_text()) == {"customer": ["client", "buyer", "my own word"]}  # still theirs


def test_a_group_whose_concept_left_the_schema_is_removed_with_its_record(dbs: Path):
    merge_synonym_glossary(DB, {"customer": ["client"], "vendor": ["supplier"]}, dbs)
    out = merge_synonym_glossary(DB, {"vendor": ["supplier"]}, dbs)
    assert out["written"] and out["removed"] == 1 and read(dbs, "semantics/glossary.json") == {"vendor": ["supplier"]}
    merge_synonym_glossary(DB, {}, dbs)
    assert read(dbs, "semantics/glossary.json") == {} and not (dbs / DB / routing.STATE_REL).exists()


def test_unusable_files_missing_folders_and_the_pinned_database_are_left_alone(dbs: Path):
    path = dbs / DB / "semantics" / "glossary.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert "left untouched" in merge_synonym_glossary(DB, {"a": ["bcde"]}, dbs)["skipped"] and path.read_text() == "[1, 2]"
    path.write_text("{broken", encoding="utf-8")
    assert merge_synonym_glossary(DB, {"a": ["bcde"]}, dbs)["skipped"] and path.read_text() == "{broken"
    path.unlink()
    assert merge_synonym_glossary(DB, {}, dbs)["unchanged"] and not path.exists()  # no empty file made
    assert "pinned" in merge_synonym_glossary("erp_main", {"a": ["bcde"]}, dbs)["skipped"]
    assert "does not exist" in merge_synonym_glossary("ghost_db", {"a": ["bcde"]}, dbs)["skipped"]


# ================================================================= the routing file


def rule(keywords: list[str], tables: list[str], auto: bool = True) -> dict[str, Any]:
    return {"keywords": keywords, "tables": tables, **({"auto": routing.GENERATED_BY} if auto else {})}


def test_a_seed_gets_both_sections_and_the_router_reads_them(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    from src.sql import table_router

    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs, raising=False)
    out = merge_routing_hints(DB, [rule(["supplier"], ["vendors"])], [rule(["supplier"], ["vendors"])], dbs)
    doc = read(dbs, "semantics/routing_hints.json")
    assert out["written"] and out["rules"] == 2 and doc["fallback_rules"][0]["auto"] == routing.GENERATED_BY
    # the router ignores the extra "auto" key
    monkeypatch.setattr(table_router, "load_routing_hints", lambda db_id="x": doc)
    assert table_router.route_tables_for_query("which supplier is late", db_id=DB, section="fallback_rules") == ["vendors"]
    assert merge_routing_hints(DB, [rule(["supplier"], ["vendors"])], [rule(["supplier"], ["vendors"])], dbs)["unchanged"]


def test_curated_rules_and_other_keys_are_kept_and_their_keywords_are_not_duplicated(dbs: Path):
    path = dbs / DB / "semantics" / "routing_hints.json"
    curated = {"keywords": ["supplier", "po"], "tables": ["my_vendor"]}
    path.write_text(json.dumps({"fallback_rules": [curated], "anchor_rules": [], "note": "mine"}), encoding="utf-8")
    merge_routing_hints(DB, [rule(["supplier", "seller"], ["vendors"])], [rule(["supplier"], ["vendors"])], dbs)
    doc = json.loads(path.read_text())
    assert doc["note"] == "mine" and doc["fallback_rules"][0] == curated
    assert doc["fallback_rules"][1]["keywords"] == ["seller"]  # "supplier" belongs to the curated rule
    assert [r["keywords"] for r in doc["anchor_rules"]] == [["supplier"]]  # sections are independent: no curated anchor rule uses it


def test_generated_rules_follow_the_schema_and_switching_off_removes_only_them(dbs: Path):
    path = dbs / DB / "semantics" / "routing_hints.json"
    curated = {"keywords": ["claims"], "tables": ["claim"]}
    path.write_text(json.dumps({"fallback_rules": [curated]}), encoding="utf-8")
    merge_routing_hints(DB, [rule(["supplier"], ["vendors"]), rule(["client"], ["customers"])], [], dbs)
    out = merge_routing_hints(DB, [rule(["supplier"], ["vendors"])], [], dbs)
    assert out["removed"] == 1 and [r["tables"] for r in json.loads(path.read_text())["fallback_rules"]] == [["claim"], ["vendors"]]
    merge_routing_hints(DB, [], [], dbs)
    assert json.loads(path.read_text()) == {"fallback_rules": [curated]}


def test_an_unknown_shape_or_a_broken_file_is_never_rewritten_and_the_seed_stays_without_rules(dbs: Path):
    path = dbs / DB / "semantics" / "routing_hints.json"
    path.write_text(json.dumps({"fallback_rules": "not a list"}), encoding="utf-8")
    out = merge_routing_hints(DB, [rule(["supplier"], ["vendors"])], [], dbs)
    assert json.loads(path.read_text()) == {"fallback_rules": "not a list"} and not out["written"]
    path.write_text("{oops", encoding="utf-8")
    assert merge_routing_hints(DB, [rule(["supplier"], ["vendors"])], [], dbs)["skipped"] and path.read_text() == "{oops"
    path.write_text("{}", encoding="utf-8")
    assert merge_routing_hints(DB, [], [], dbs)["unchanged"] and path.read_text() == "{}"
    assert "pinned" in merge_routing_hints("erp_main", [], [], dbs)["skipped"]


# ================================================================= the phase


@pytest.mark.asyncio
async def test_the_phase_fills_both_files_and_the_prompt_and_router_see_them_at_once(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    from src.sql import prompt_builder, table_router

    put(dbs)
    monkeypatch.setattr("src.sql.knowledge.loaders.DATABASES_DIR", dbs)
    prompt_builder.clear_prompt_caches(DB)
    table_router.load_routing_hints.cache_clear(DB)
    assert prompt_builder.load_glossary(DB) == "" and table_router.route_anchor_tables("which supplier is late", db_id=DB) == set()  # caches now hold "empty"

    report = await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    assert report["ok"] and report["glossary"]["groups"] == 2 and report["routing"]["rules"] >= 2
    glossary, hints = read(dbs, "semantics/glossary.json"), read(dbs, "semantics/routing_hints.json")
    assert "supplier" in glossary["vendor"] and any("supplier" in r["keywords"] and r["tables"] == ["vendors"] for r in hints["anchor_rules"])
    # the pipeline's own readers see the new text immediately (the phase cleared the caches that held the old, empty answer)
    assert "- vendor: " in prompt_builder.load_glossary(DB) and "supplier" in prompt_builder.load_glossary(DB)
    assert table_router.route_anchor_tables("which supplier is late", db_id=DB) == {"vendors"}
    assert table_router.route_tables_for_query("list our clients", db_id=DB, section="fallback_rules") == ["customers"]
    assert table_router.route_anchor_tables("show vendors by total", db_id=DB) == set()  # words the names already contain add nothing

    again = await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    assert again["glossary"]["unchanged"] and again["routing"]["unchanged"]


@pytest.mark.asyncio
async def test_without_a_catalog_or_for_the_pinned_database_nothing_is_generated(dbs: Path):
    out = await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    assert out["ok"] and "no catalog" in out["skipped"] and read(dbs, "semantics/glossary.json") == {}
    assert "pinned" in (await hints_phase.run_hints_phase(db_id="erp_main", inject=True, databases_dir=dbs))["skipped"]


@pytest.mark.asyncio
async def test_with_injection_off_the_generated_part_is_taken_back_out(dbs: Path):
    put(dbs)
    path = dbs / DB / "semantics" / "glossary.json"
    await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    path.write_text(json.dumps({**json.loads(path.read_text()), "mine": ["x"]}), encoding="utf-8")
    out = await hints_phase.run_hints_phase(db_id=DB, inject=False, databases_dir=dbs)
    assert out["injection"] == "off" and json.loads(path.read_text()) == {"mine": ["x"]}
    assert read(dbs, "semantics/routing_hints.json") == {"fallback_rules": [], "anchor_rules": []}


@pytest.mark.asyncio
async def test_copies_and_test_tables_are_not_routed_to(dbs: Path):
    extra = (table("vendors_bak", {"vendor_id": INT}), table("vendor_tmp", {"x": INT}))
    put(dbs, *shop(), *extra)
    g = load_graph(DB, dbs)
    profiles = {"generated_by": "auto_table_profiles", "inputs_hash": g.inputs_hash,
                "tables": {"vendors_bak": {"authority": "secondary", "tags": ["backup"]}, "vendor_tmp": {"authority": "standalone", "tags": ["temp"]}}}
    put(dbs, *shop(), *extra, profiles=profiles)
    await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    rules = read(dbs, "semantics/routing_hints.json")["anchor_rules"]
    assert all("vendors_bak" not in r["tables"] and "vendor_tmp" not in r["tables"] for r in rules)


@pytest.mark.asyncio
async def test_a_failure_becomes_a_report_and_never_an_exception(dbs: Path, monkeypatch: pytest.MonkeyPatch):
    put(dbs)
    monkeypatch.setattr(hints_phase, "build_hints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = await hints_phase.run_hints_phase(db_id=DB, inject=True, databases_dir=dbs)
    assert out["ok"] is False and "boom" in out["error"]


# ================================================================= through the real sync (real SQLite)

@pytest.mark.asyncio
async def test_a_real_sync_fills_the_glossary_and_routing_hints_that_stayed_empty_before(shop_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch):
    _flags(monkeypatch, "auto_catalog", "auto_prompt_inject")
    assert read(dbs, "semantics/glossary.json") == {} and read(dbs, "semantics/routing_hints.json") == {}
    result = await _sync()
    assert result["routing_hints"]["ok"] and result["routing_hints"]["glossary"]["groups"] >= 1
    glossary, hints = read(dbs, "semantics/glossary.json"), read(dbs, "semantics/routing_hints.json")
    assert "client" in glossary["customer"]  # the fixture database has a customer table
    assert any(r["tables"] == ["customer"] and "buyer" in r["keywords"] for r in hints["anchor_rules"])
    second = await _sync()  # a second sync of an unchanged schema rewrites nothing
    assert second["routing_hints"]["glossary"]["unchanged"] and second["routing_hints"]["routing"]["unchanged"]


@pytest.mark.asyncio
async def test_a_sync_follows_the_schema_when_a_table_goes_away(shop_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch):
    _flags(monkeypatch, "auto_catalog", "auto_prompt_inject")
    await _sync()
    assert "customer" in read(dbs, "semantics/glossary.json")
    _sql(shop_db, "DROP TABLE order_items", "DROP TABLE orders", "DROP TABLE customer", "CREATE TABLE vendor_list(id INTEGER PRIMARY KEY, vendor_ref TEXT)")
    await _sync()
    glossary = read(dbs, "semantics/glossary.json")
    assert "customer" not in glossary and "vendor" in glossary
    assert all("customer" not in r["tables"] for r in read(dbs, "semantics/routing_hints.json")["anchor_rules"])


@pytest.mark.asyncio
async def test_without_injection_a_sync_leaves_the_files_as_they_were_and_a_phase_failure_never_fails_the_sync(shop_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch):
    _flags(monkeypatch, "auto_catalog")
    result = await _sync()
    assert result["status"] == "ok" and result["routing_hints"]["injection"] == "off"
    assert read(dbs, "semantics/glossary.json") == {} and read(dbs, "semantics/routing_hints.json") == {}
    _flags(monkeypatch, "auto_catalog", "auto_prompt_inject")
    monkeypatch.setattr(hints_phase, "build_hints", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = await _sync()
    assert failed["status"] == "ok" and failed["routing_hints"]["ok"] is False


@pytest.mark.asyncio
async def test_a_sync_without_the_catalog_phase_does_not_touch_the_files(shop_db: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch):
    _flags(monkeypatch)  # every flag off
    result = await _sync()
    assert "routing_hints" not in result and read(dbs, "semantics/glossary.json") == {}


def test_a_failing_routing_hints_phase_does_not_make_the_sync_record_untrusted(dbs: Path):
    from src.sql.sync import drift, orchestrate

    fps = {"a": "1"}
    info = {"schema_hash": "h", "structure_hash": drift.structure_hash(fps), "table_fingerprints": fps, "schema_complete": True, "enabled": ["catalog"], "phase_versions": {}}
    orchestrate.persist_generation({"routing_hints": {"ok": False, "error": "x"}}, info, None, DB, committed=True)
    state = drift.load_state(DB)
    assert state["phases"]["routing_hints"] == "failed" and state["clean"] is True
    orchestrate.persist_generation({"table_profiles": {"ok": False, "error": "x"}}, info, None, DB, committed=True)
    assert drift.load_state(DB)["clean"] is False  # a phase later phases depend on still does
