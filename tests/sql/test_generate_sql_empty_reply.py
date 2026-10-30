"""A model that returns no text is a failed call (reported as an infra error), not an abstention."""

from unittest.mock import AsyncMock

import pytest

import src.sql.generation as gen
from src.sql.dialects.sqlite import SQLITE_PROFILE

_SCHEMA = "CREATE TABLE party (id INT, name TEXT, deleted_at TIMESTAMP);"


@pytest.mark.parametrize("reply", ["", "   \n  ", None])
async def test_empty_reply_is_reported_as_infra_error(reply):
    router = AsyncMock()
    router.chat.return_value = reply
    sql, cot, infra = await gen.generate_sql(router=router, query="List parties", schema=_SCHEMA, dialect=SQLITE_PROFILE, db_id="erp_main")
    assert sql == "" and cot is None
    assert infra == "empty_llm_response"


async def test_no_sql_abstention_is_not_an_infra_error():
    router = AsyncMock()
    router.chat.return_value = "NO_SQL - nothing in the schema relates to this."
    sql, _cot, infra = await gen.generate_sql(router=router, query="weather?", schema=_SCHEMA, dialect=SQLITE_PROFILE, db_id="erp_main")
    assert sql == "" and infra is None


async def test_prose_without_sql_is_not_an_infra_error():
    router = AsyncMock()
    router.chat.return_value = "I think you should look at the party table."
    sql, _cot, infra = await gen.generate_sql(router=router, query="parties", schema=_SCHEMA, dialect=SQLITE_PROFILE, db_id="erp_main")
    assert sql == "" and infra is None
