"""Table-authority hints (auto-schema Phase 12, part 2): the index, the planner, the mode switch, retrieval and the prompt.

Nothing here names a real table or question: the schemas are synthetic copy/real pairs.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.sql.prompt_builder as pb
import src.sql.schema_retrieval as sr
from src.core.config import settings
from src.sql import registry
from src.sql.profiles import hints as h

DB = "auth_db"
_DIALECT = SimpleNamespace(name="T-SQL", date_functions="")


# ----------------------------------------------------------------------------------------------- file helpers


def _entry(name: str, authority: str = "standalone", preferred: str | None = None, confidence: str | None = None, schema: str = "dbo") -> dict[str, Any]:
    return {"name": name, "schema": schema, "kind": "table", "tags": [], "tag_basis": None, "group": None,
            "authority": authority, "preferred": preferred, "confidence": confidence}


def _doc(tables: dict[str, dict[str, Any]], generated_by: str | None = "auto_table_profiles") -> dict[str, Any]:
    doc: dict[str, Any] = {"input_key": "k", "tables": tables, "groups": []}
    if generated_by:
        doc["generated_by"] = generated_by
    return doc


def _pair_doc(confidence: str = "high") -> dict[str, Any]:
    return _doc({
        "dbo.item": _entry("item", "primary", None, confidence),
        "dbo.item_bak": _entry("item_bak", "secondary", "dbo.item", confidence),
        "dbo.other": _entry("other"),
    })


def _write(root: Path, doc: Any, db: str = DB, raw: str | None = None) -> Path:
    folder = root / db / "schema"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "table_profiles.json"
    path.write_text(raw if raw is not None else json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture()
def dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "DATABASES_DIR", tmp_path)
    h.clear_authority_cache()
    yield tmp_path
    h.clear_authority_cache()


def _flags(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool], apply_dbs: str = "") -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda f, db: flags.get(f, False))
    monkeypatch.setattr(settings, "prompt_hints_apply_dbs", apply_dbs)


# ----------------------------------------------------------------------------------------------- the index


def test_a_decided_copy_points_at_its_primary(dbs: Path) -> None:
    _write(dbs, _pair_doc())
    idx = h.load_authority_index(DB)
    assert idx is not None and idx.primary_for("ITEM_BAK") == "item" and idx.primary_for("item") is None


def test_medium_confidence_counts_and_low_or_missing_confidence_does_not(dbs: Path) -> None:
    _write(dbs, _pair_doc("medium"))
    assert h.load_authority_index(DB).primary_for("item_bak") == "item"
    h.clear_authority_cache()
    _write(dbs, _pair_doc("low"))
    assert h.load_authority_index(DB) is None


@pytest.mark.parametrize("authority", ["undecided", "equal", "standalone", "primary"])
def test_only_a_decided_secondary_is_used(dbs: Path, authority: str) -> None:
    _write(dbs, _doc({"dbo.item": _entry("item", "primary", None, "high"), "dbo.item_bak": _entry("item_bak", authority, "dbo.item", "high")}))
    assert h.load_authority_index(DB) is None


def test_a_hand_written_or_broken_or_missing_file_gives_none(dbs: Path) -> None:
    assert h.load_authority_index(DB) is None  # no file
    _write(dbs, _pair_doc(), raw="{broken")
    assert h.load_authority_index(DB) is None
    h.clear_authority_cache()
    _write(dbs, _doc(_pair_doc()["tables"], generated_by=None))
    assert h.load_authority_index(DB) is None  # no "generated_by": curated, not ours
    h.clear_authority_cache()
    _write(dbs, ["not", "a", "mapping"])
    assert h.load_authority_index(DB) is None


def test_a_name_in_two_schemas_is_ambiguous_and_skipped(dbs: Path) -> None:
    _write(dbs, _doc({
        "dbo.item": _entry("item", "primary", None, "high"),
        "hr.item": _entry("item", "standalone", schema="hr"),
        "dbo.item_bak": _entry("item_bak", "secondary", "dbo.item", "high"),
    }))
    assert h.load_authority_index(DB) is None


def test_a_secondary_whose_target_is_not_a_primary_is_skipped(dbs: Path) -> None:
    _write(dbs, _doc({"dbo.item": _entry("item"), "dbo.item_bak": _entry("item_bak", "secondary", "dbo.item", "high")}))
    assert h.load_authority_index(DB) is None


def test_the_pinned_database_is_never_read(dbs: Path) -> None:
    _write(dbs, _pair_doc(), db="erp_main")
    assert h.load_authority_index("erp_main") is None


def test_the_index_follows_the_file_when_it_changes(dbs: Path) -> None:
    path = _write(dbs, _pair_doc())
    assert h.load_authority_index(DB) is not None
    path.write_text(json.dumps(_doc({"dbo.x": _entry("x")})) + " ", encoding="utf-8")
    assert h.load_authority_index(DB) is None


# ----------------------------------------------------------------------------------------------- the planner


def _idx(dbs: Path) -> h.AuthorityIndex:
    _write(dbs, _pair_doc())
    idx = h.load_authority_index(DB)
    assert idx is not None
    return idx


def test_the_primary_is_offered_when_only_the_copy_was_chosen(dbs: Path) -> None:
    plan = h.plan_authority(["item_bak", "other"], DB, index=_idx(dbs), available={"item", "item_bak", "other"})
    assert plan.add == ("item",) and plan.pairs == (("item_bak", "item"),)


def test_nothing_is_added_when_the_primary_is_already_chosen_or_not_in_the_live_schema(dbs: Path) -> None:
    idx = _idx(dbs)
    assert h.plan_authority(["item_bak", "item"], DB, index=idx, available={"item", "item_bak"}).add == ()
    missing = h.plan_authority(["item_bak"], DB, index=idx, available={"item_bak"})
    assert missing.add == () and dict(missing.skipped) == {"missing_table": 1}


def test_the_number_of_added_tables_is_capped(dbs: Path) -> None:
    idx = h.AuthorityIndex({f"c{i}": f"p{i}" for i in range(6)})
    names = {f"c{i}" for i in range(6)} | {f"p{i}" for i in range(6)}
    plan = h.plan_authority([f"c{i}" for i in range(6)], DB, index=idx, available=names)
    assert plan.add == ("p0", "p1", "p2") and dict(plan.skipped) == {"over_cap": 3}


def test_a_note_needs_both_tables_in_the_prompt(dbs: Path) -> None:
    plan = h.plan_authority(["item_bak"], DB, index=_idx(dbs), available={"item", "item_bak"})
    assert plan.note_lines(["item_bak"]) == []  # the primary was cut by the budget: no dangling note
    (line,) = plan.note_lines(["item_bak", "item"])
    assert "`item_bak`" in line and "`item`" in line and "copy" in line


def test_no_profile_file_means_no_plan(dbs: Path) -> None:
    assert h.plan_authority(["item_bak"], DB, available={"item"}) is None


# ----------------------------------------------------------------------------------------------- the mode switch


@pytest.mark.parametrize(
    ("flags", "apply_dbs", "expected"),
    [
        ({}, "", ""),
        ({}, DB, ""),  # being listed does nothing without the Phase 12 flags
        ({"auto_prompt_shadow": True}, "", "shadow"),
        ({"auto_prompt_shadow": True}, DB, "shadow"),  # apply needs auto_prompt_inject too
        ({"auto_prompt_inject": True}, "", "shadow"),  # route bridges on does NOT switch these on
        ({"auto_prompt_inject": True}, "other_db", "shadow"),
        ({"auto_prompt_inject": True}, DB, "apply"),
        ({"auto_prompt_inject": True}, f" {DB.upper()} , x", "apply"),
        ({"auto_prompt_inject": True}, "*", "apply"),
    ],
)
def test_mode_switch(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool], apply_dbs: str, expected: str) -> None:
    _flags(monkeypatch, flags, apply_dbs)
    assert h.hints_mode(DB) == expected


def test_the_pinned_database_never_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "prompt_hints_apply_dbs", "*")
    assert h.hints_mode("erp_main") == ""  # real flag lookup: erp_main is pinned off


# ----------------------------------------------------------------------------------------------- the prompt


def _compact(*names: str) -> str:
    return "\n\n".join(f"{n}: {n}_id(int, PK), name(varchar)" for n in names)


def _prompt(schema: str) -> str:
    return pb.build_sql_prompt("q", schema, _DIALECT, db_id=DB)


@pytest.fixture()
def quiet_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")


def test_prompt_is_unchanged_in_shadow_mode_and_gets_a_note_in_apply_mode(dbs: Path, monkeypatch: pytest.MonkeyPatch, quiet_prompt: None) -> None:
    _write(dbs, _pair_doc())
    schema = _compact("item_bak", "item", "other")

    _flags(monkeypatch, {"auto_prompt_inject": True})  # route bridges on, hints not listed: shadow
    baseline = _prompt(schema)
    assert "Table notes" not in baseline

    _flags(monkeypatch, {"auto_prompt_shadow": True})  # shadow flag only
    shadow_only = _prompt(schema)
    assert "Table notes" not in shadow_only

    _flags(monkeypatch, {"auto_prompt_inject": True}, apply_dbs=DB)
    on = _prompt(schema)
    block = re.search(r"\n\nTable notes \(similar tables\):\n- `item_bak` looks like a copy of `item`\.[^\n]*", on)
    assert block is not None
    assert on.replace(block.group(0), "", 1) == baseline  # the note block is the ONLY difference


def test_flags_off_gives_the_same_prompt_as_before_this_part(dbs: Path, monkeypatch: pytest.MonkeyPatch, quiet_prompt: None) -> None:
    _write(dbs, _pair_doc())
    _flags(monkeypatch, {}, apply_dbs=DB)  # listed, but the Phase 12 flags are off
    assert "Table notes" not in _prompt(_compact("item_bak", "item"))


def test_no_note_when_the_pair_is_not_both_in_the_prompt(dbs: Path, monkeypatch: pytest.MonkeyPatch, quiet_prompt: None) -> None:
    _write(dbs, _pair_doc())
    _flags(monkeypatch, {"auto_prompt_inject": True}, apply_dbs=DB)
    assert "Table notes" not in _prompt(_compact("item_bak", "other"))


def test_shadow_mode_records_what_would_be_said(dbs: Path, monkeypatch: pytest.MonkeyPatch, quiet_prompt: None) -> None:
    _write(dbs, _pair_doc())
    events: list[dict[str, Any]] = []
    monkeypatch.setattr("src.utils.telemetry.log_telemetry", lambda **kw: events.append(kw))
    _flags(monkeypatch, {"auto_prompt_shadow": True})
    _prompt(_compact("item_bak", "item"))
    shadow = [e for e in events if e["stage"] == "table_authority_shadow"]
    assert shadow and "item_bak" in shadow[0]["extra"]["notes"][0]


def test_a_hints_crash_never_breaks_the_prompt(dbs: Path, monkeypatch: pytest.MonkeyPatch, quiet_prompt: None) -> None:
    _write(dbs, _pair_doc())
    schema = _compact("item_bak", "item")
    _flags(monkeypatch, {"auto_prompt_inject": True})
    off = _prompt(schema)
    _flags(monkeypatch, {"auto_prompt_inject": True}, apply_dbs=DB)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(h, "plan_authority", boom)
    assert _prompt(schema) == off


# ----------------------------------------------------------------------------------------------- retrieval


def _ddl(name: str) -> str:
    return f"TABLE {name} (\n  {name}_id int,\n  name varchar\n)"


class _Emb:
    async def embed_query(self, q: str) -> tuple[list[float], dict]:
        return [0.0], {}


def _store(tables: dict[str, str], hits: list[str]) -> Any:
    class _Chunk:
        def __init__(self, t: str) -> None:
            self.content, self.document_id, self.db_id, self.metadata = tables[t], f"schema:{DB}", DB, {"db_id": DB}

    class _Hit:
        def __init__(self, t: str) -> None:
            self.chunk = _Chunk(t)

    class _Store:
        async def search_hybrid(self, **_kw: Any) -> list[Any]:
            return [_Hit(t) for t in hits]

    return _Store()


def _world(monkeypatch: pytest.MonkeyPatch, flags: dict[str, bool], apply_dbs: str = "") -> tuple[dict[str, str], str]:
    tables = {t: _ddl(t) for t in ("item", "item_bak", "other", "more")}
    monkeypatch.setattr(pb, "get_raw_relationships", lambda db_id=DB: [])
    monkeypatch.setattr(pb, "build_column_glossary_for_query", lambda q, db_id=DB: "")
    monkeypatch.setattr(sr, "route_anchor_tables", lambda q, db_id=DB: set())
    monkeypatch.setattr(sr, "is_feature_enabled", lambda f: True)
    _flags(monkeypatch, flags, apply_dbs)
    return tables, "\n\n".join(tables.values())


def _retrieve(tables: dict[str, str], full: str) -> str:
    return asyncio.run(sr.retrieve_schema_from_qdrant("q", full, _store(tables, ["item_bak", "other"]), _Emb(), db_id=DB))


def _present(out: str) -> set[str]:
    return set(re.findall(r"(?im)^(?:table\s+)?(\w+)\s*(?:\(|:)", out))


def test_retrieval_offers_the_real_table_only_in_apply_mode(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(dbs, _pair_doc())
    tables, full = _world(monkeypatch, {})
    off = _retrieve(tables, full)
    assert "item_bak" in _present(off) and "item" not in _present(off)  # the failure this part addresses

    tables, full = _world(monkeypatch, {"auto_prompt_shadow": True, "auto_prompt_inject": True})
    assert _retrieve(tables, full) == off  # flags on, hints not listed: shadow, identical

    tables, full = _world(monkeypatch, {"auto_prompt_inject": True}, apply_dbs=DB)
    on = _retrieve(tables, full)
    assert {"item_bak", "item", "other"} <= _present(on)  # the copy stays, the real table is added


def test_retrieval_is_identical_without_a_profile_file_and_when_the_planner_crashes(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tables, full = _world(monkeypatch, {})
    off = _retrieve(tables, full)
    tables, full = _world(monkeypatch, {"auto_prompt_inject": True}, apply_dbs=DB)
    assert _retrieve(tables, full) == off  # no table_profiles.json

    _write(dbs, _pair_doc())

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(h, "plan_authority", boom)
    assert _retrieve(tables, full) == off


def test_compaction_telemetry_names_tables_that_could_not_be_compacted(dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tables, _full = _world(monkeypatch, {})
    tables["other"] = "TABLE other (\n  Cost Recovery % nvarchar,\n  a/b int\n)"  # names the parser cannot read
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(sr, "log_telemetry", lambda **kw: events.append(kw))
    asyncio.run(sr.retrieve_schema_from_qdrant("q", "\n\n".join(tables.values()), _store(tables, ["item_bak", "other"]), _Emb(), db_id=DB))
    compaction = [e for e in events if e["stage"] == "schema_compaction_applied"]
    assert compaction and compaction[0]["extra"]["uncompacted_tables"] == ["other"]
