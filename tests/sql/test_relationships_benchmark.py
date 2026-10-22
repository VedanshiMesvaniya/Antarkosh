"""Phase 6 / step 6i: the benchmark on five real catalogs, pinned.

Each catalog in ``tests/sql/fixtures/relationships`` was written by this project's own catalog phase against a live
PostgreSQL or MariaDB server. Every declared foreign key is hidden, the rules run on what is left, and the result is
compared with the answer sheet. The expected misses and extras below are listed by name on purpose: if a rule change
makes the numbers better OR worse, this test fails and a person has to look at the pair and decide.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.db import relationships_benchmark as cli
from src.sql.relationships.benchmark import (
    DEFAULT_THRESHOLDS,
    filter_tables,
    hide_foreign_keys,
    live_check,
    run_case,
    total,
    truth_pairs,
)
from src.sql.relationships.writer import HIGH_THRESHOLD, MEDIUM_THRESHOLD

FIXTURES = Path(__file__).parent / "fixtures" / "relationships"
NAMES = ("employees", "pagila", "northwind", "chinook", "sakila")

# Declared keys the rules cannot find, and why. None of these names says what it points at.
MISSED = {
    "employees": [],
    "pagila": [],
    "northwind": [
        ("employees", "reports_to", "employees", "employee_id"),  # a self reference called reports_to: no id suffix, no hierarchy word
        ("orders", "ship_via", "shippers", "shipper_id"),  # the column name does not mention the table
    ],
    "chinook": [
        ("customer", "support_rep_id", "employee", "employee_id"),  # support_rep names a role, not a table
        ("employee", "reports_to", "employee", "employee_id"),
    ],
    "sakila": [],
}
# Relationships the rules propose that the database does not declare. Each one was read by a person and is a genuine relationship.
EXTRA = {
    "employees": [],
    "pagila": [
        ("payment", "customer_id", "customer", "customer_id"),  # payment is a partitioned parent: its keys are declared on the partitions
        ("payment", "rental_id", "rental", "rental_id"),
        ("payment", "staff_id", "staff", "staff_id"),
        ("store", "manager_staff_id", "staff", "staff_id"),  # the store's manager is a staff member; the database never declared it
    ],
    "northwind": [],
    "chinook": [],
    "sakila": [("film_text", "film_id", "film", "film_id")],  # film_text is the full-text copy of film
}


def _load(name: str) -> tuple[dict, dict]:
    return (json.loads((FIXTURES / f"{name}.schema.json").read_text(encoding="utf-8")), json.loads((FIXTURES / f"{name}.inventory.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def results():
    return {n: run_case(n, *_load(n)) for n in NAMES}


@pytest.mark.parametrize("name", NAMES)
def test_the_missed_and_the_undeclared_proposals_are_exactly_the_known_ones(name, results):
    r = results[name]
    assert r.missed == sorted(MISSED[name]) and r.extra == sorted(EXTRA[name])


def test_overall_recall_and_precision_at_the_review_threshold(results):
    m = total(list(results.values()), MEDIUM_THRESHOLD)
    assert (m.tp, m.fn, m.fp) == (67, 4, 5)
    assert m.recall >= 0.93 and m.precision >= 0.92


def test_without_a_declared_key_or_procedure_nothing_reaches_the_threshold_for_prompts(results):
    assert total(list(results.values()), HIGH_THRESHOLD).tp + total(list(results.values()), HIGH_THRESHOLD).fp == 0


def test_recall_never_rises_and_never_falls_below_the_pin_as_the_threshold_goes_up(results):
    recalls = [total(list(results.values()), t).recall for t in sorted(DEFAULT_THRESHOLDS)]
    assert recalls == sorted(recalls, reverse=True) and recalls[0] >= 0.93


def test_every_rule_that_fired_is_right_far_more_often_than_not(results):
    by_rule: dict[str, list[int]] = {}
    for r in results.values():
        for rule, (declared, other) in r.by_rule().items():
            by_rule.setdefault(rule, [0, 0])
            by_rule[rule][0] += declared
            by_rule[rule][1] += other
    assert {"table_id", "same_name", "pk_name", "role_prefix", "junction", "indexed"} <= set(by_rule)
    for rule, (declared, other) in by_rule.items():
        assert declared / (declared + other) >= 0.75, rule


@pytest.mark.parametrize("name", NAMES)
def test_keeping_the_declared_keys_finds_every_one_with_full_confidence(name):
    r = run_case(name, *_load(name), hide=False)
    assert r.missed == [] and all(r.predicted[p].score == 1.0 for p in r.truth)


# ---------------------------------------------------------------- the helpers

def test_hiding_keys_removes_them_and_leaves_the_input_untouched():
    schema, _ = _load("sakila")
    before = json.dumps(schema, sort_keys=True)
    hidden = hide_foreign_keys(schema)
    assert hidden["relationships"] == [] and not any(c.get("is_foreign_key") for t in hidden["tables"] for c in t["columns"])
    assert json.dumps(schema, sort_keys=True) == before and truth_pairs(schema) and not truth_pairs(hidden)


def test_excluding_tables_drops_them_and_every_key_that_touches_them():
    schema, _ = _load("sakila")
    doc = filter_tables(schema, r"^film_")
    assert not any(t["name"].startswith("film_") for t in doc["tables"])
    assert not any(r["from_table"].startswith("film_") or r["to_table"].startswith("film_") for r in doc["relationships"])
    assert len(schema["tables"]) > len(doc["tables"])


# ---------------------------------------------------------------- live check and the command line

def test_live_check_counts_what_the_phase_would_do_and_writes_nothing(tmp_path: Path):
    folder = tmp_path / "northwind" / "schema"
    folder.mkdir(parents=True)
    schema, inv = _load("northwind")
    (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (folder / "table_inventory.json").write_text(json.dumps(inv), encoding="utf-8")
    before = sorted(p.name for p in folder.iterdir())
    out = live_check("northwind", tmp_path)
    assert out["tables"] == 14 and out["declared_keys"] == 13 and out["edges"] == 13 and out["publishable"] == 13 and out["held_back"] == {}
    assert out == live_check("northwind", tmp_path) and sorted(p.name for p in folder.iterdir()) == before
    assert live_check("nothing_here", tmp_path) is None


def test_the_command_line_reports_on_the_shipped_fixtures_and_writes_nothing(capsys):
    before = sorted(p.name for p in FIXTURES.iterdir())
    assert cli.main([]) == 0
    text = capsys.readouterr().out
    assert "all databases together" in text and "per rule" in text and "pagila" in text
    assert cli.main(["--json", "--thresholds", "0.45"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data["databases"]) == set(NAMES) and data["total"]["0.45"]["tp"] == 67
    assert sorted(p.name for p in FIXTURES.iterdir()) == before


def test_the_command_line_handles_a_missing_database_and_bad_usage(tmp_path: Path, capsys):
    assert cli.main(["--db-id", "ghost", "--databases-dir", str(tmp_path)]) == 2
    assert cli.main(["--live-check", "--db-id", "ghost", "--databases-dir", str(tmp_path)]) == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--live-check"])
    assert exc.value.code == 2
    capsys.readouterr()
