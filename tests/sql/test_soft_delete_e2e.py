"""Soft-delete knowledge end to end on a real database: the catalog a real sync writes -> the columns found -> SQL that really filters."""

# ruff: noqa: F811, F401
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.sql import soft_delete as sd
from src.sql import soft_delete_spec as ss
from tests.sql.test_catalog_extraction import _sync_args, dbs, sqlite_db
from tests.sql.test_resync_e2e import DB, _sync, shop


def _flags(monkeypatch: pytest.MonkeyPatch, *on: str) -> None:
    monkeypatch.setattr("src.utils.feature_flags.is_db_feature_enabled", lambda flag, db: flag in on)


@pytest.fixture()
def marked(shop: Path) -> Path:
    """The shop database with a different kind of soft-delete marker on each table."""
    con = sqlite3.connect(shop)
    con.executescript(
        """
        ALTER TABLE customer    ADD COLUMN deleted_at DATETIME;
        ALTER TABLE orders      ADD COLUMN IsDeleted INTEGER DEFAULT 0;
        ALTER TABLE order_items ADD COLUMN del_flag CHAR(1) DEFAULT 'N';
        INSERT INTO customer(id, email, name, deleted_at) VALUES (3, 'c@x.com', 'C', '2024-05-05');
        INSERT INTO orders(id, customer_id, total, IsDeleted) VALUES (1, 1, 10, 0), (2, 1, 20, 1), (3, 2, 30, NULL), (4, 3, 40, 0);
        INSERT INTO order_items(order_id, line_no, sku, del_flag) VALUES (1, 1, 's1', 'N'), (1, 2, 's2', 'Y'), (2, 1, 's3', 'N'), (3, 1, 's4', NULL), (4, 1, 's5', 'n');
        """
    )
    con.commit()
    con.close()
    return shop


@pytest.mark.asyncio
async def test_the_columns_are_found_from_the_catalog_a_real_sync_wrote(marked: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_scaffold", "auto_catalog")
    await _sync()
    ss.clear_spec_cache()
    specs = ss.load_specs(DB)
    assert {t: (s.column, s.kind, s.source) for t, s in specs.items()} == {
        "customer": ("deleted_at", "timestamp", "declared"),  # the template's db.yaml declares deleted_at
        "orders": ("IsDeleted", "int_flag", "discovered"),
        "order_items": ("del_flag", "char_flag", "discovered"),
    }


@pytest.mark.asyncio
async def test_with_the_flag_on_the_generated_sql_returns_only_live_rows_and_with_it_off_nothing_changes(marked: Path, dbs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _flags(monkeypatch, "auto_scaffold", "auto_catalog")
    await _sync()
    ss.clear_spec_cache()
    con = sqlite3.connect(marked)
    sql = "SELECT o.id, c.id, i.sku FROM orders o JOIN customer c ON c.id = o.customer_id JOIN order_items i ON i.order_id = o.id"
    everything = con.execute(sql).fetchall()

    off = sd.enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "sqlite", DB)
    assert con.execute(off).fetchall() == everything  # flag off: the old behaviour (no filtering for these markers)

    _flags(monkeypatch, "auto_scaffold", "auto_catalog", "auto_soft_delete_apply")
    on = sd.enforce_soft_delete_filter(sql, "ACTIVE_ONLY", "sqlite", DB)
    live = sorted(con.execute(on).fetchall())
    # worked out independently, with the plain conditions for each marker:
    expected = sorted(
        con.execute(
            "SELECT o.id, c.id, i.sku FROM orders o JOIN customer c ON c.id = o.customer_id JOIN order_items i ON i.order_id = o.id "
            "WHERE (o.IsDeleted = 0 OR o.IsDeleted IS NULL) AND c.deleted_at IS NULL AND (i.del_flag IN ('N', 'n') OR i.del_flag IS NULL)"
        ).fetchall()
    )
    assert live == expected == [(1, 1, "s1"), (3, 2, "s4")]
    assert len(everything) == 5 and set(live) < set(everything)  # four rows were hidden (deleted order, deleted item, deleted customer's order...)

    only_deleted = con.execute(sd.enforce_soft_delete_filter("SELECT id FROM orders", "DELETED_ONLY", "sqlite", DB)).fetchall()
    assert {r[0] for r in only_deleted} == {2}
    con.close()
