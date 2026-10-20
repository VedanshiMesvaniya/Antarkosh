"""Find and parse the SQL statements inside stored-routine text (auto-schema Phase 5, step 1).

SQLGlot cannot parse a whole procedure: T-SQL, MySQL and PL/SQL bodies fail outright and a PostgreSQL
body is one opaque string. So the routine text is TOKENISED, the start of every statement is found
(SELECT / INSERT / UPDATE / DELETE / MERGE / WITH / CREATE TABLE at parenthesis depth 0), each chunk
is cut where the statement really ends, and every chunk is parsed on its own. A chunk that does not
parse is simply counted, never fatal. Nothing here executes SQL or touches a database.

Origin: new (no previous file)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ErrorLevel
from sqlglot.tokens import TokenType as TT

# Engine key (as used everywhere in this project) -> SQLGlot dialect name.
DIALECTS: dict[str, str] = {
    "postgresql": "postgres",
    "mysql": "mysql",
    "mssql": "tsql",
    "oracle": "oracle",
    "sqlite": "sqlite",
}

MAX_CHUNK_SPAN = 4  # a statement may be several start-points (INSERT .. SELECT, WITH .. SELECT, MERGE .. INSERT)

_START = frozenset({TT.SELECT, TT.INSERT, TT.UPDATE, TT.DELETE, TT.MERGE, TT.WITH})
# A SELECT/INSERT right after one of these continues the previous statement instead of starting a new one.
_GLUE_PREV = frozenset({TT.UNION, TT.INTERSECT, TT.EXCEPT, TT.ALL, TT.INTO, TT.L_PAREN, TT.COMMA, TT.ON, TT.DISTINCT})
# String literals and quoted identifiers can spell a block word ('OPEN', [End]); they never end a statement.
_QUOTED = frozenset(
    getattr(TT, n) for n in (
        "STRING", "NATIONAL_STRING", "RAW_STRING", "HEREDOC_STRING", "BYTE_STRING", "BIT_STRING", "HEX_STRING",
        "UNICODE_STRING", "IDENTIFIER",
    ) if hasattr(TT, n)
)
_TABLE_WORDS = frozenset({"TABLE", "TEMP", "TEMPORARY", "GLOBAL", "LOCAL", "UNLOGGED"})
# Words that end a statement when they appear at depth 0 without a semicolon (T-SQL has none).
_STOP = frozenset({
    "END", "ELSE", "BEGIN", "IF", "WHILE", "DECLARE", "SET", "EXEC", "EXECUTE", "PRINT", "RETURN", "DROP", "CLOSE",
    "OPEN", "FETCH", "DEALLOCATE", "GO", "COMMIT", "ROLLBACK", "RAISERROR", "THROW", "BREAK", "CONTINUE", "LOOP",
    "EXCEPTION", "OPTION",
})

_HINT_WORD = r"(?:NOLOCK|READUNCOMMITTED|READCOMMITTED|HOLDLOCK|ROWLOCK|UPDLOCK|READPAST|TABLOCKX?|NOWAIT|XLOCK|PAGLOCK|INDEX\s*(?:\([^)]*\)|=\s*\w+))"
_TSQL_HINT = re.compile(rf"\bWITH\s*\(\s*{_HINT_WORD}(?:\s*,\s*{_HINT_WORD})*\s*\)", re.IGNORECASE)
_TSQL_OPTION = re.compile(r"\bOPTION\s*\([^)]*\)", re.IGNORECASE)
_PG_BODY = re.compile(r"\$(\w*)\$(.*)\$\1\$", re.DOTALL)
_PG_PERFORM = re.compile(r"\bPERFORM\b", re.IGNORECASE)
# SQL assembled in a string at run time cannot be analysed; it is only counted.
_DYNAMIC = re.compile(r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(|\bEXEC(?:UTE)?\s+@|\bEXECUTE\s+IMMEDIATE\b|\bEXECUTE\s+format\s*\(", re.IGNORECASE)


@dataclass
class Split:
    """The statements found in one routine."""

    trees: list[exp.Expression] = field(default_factory=list)
    found: int = 0  # statements attempted
    parsed: int = 0  # statements that parsed
    dynamic: int = 0  # dynamic-SQL calls (counted, never analysed)
    truncated: bool = False  # stopped early (time budget or statement cap)
    error: str | None = None  # the routine could not even be tokenised


def prepare_text(text: str, dialect: str) -> str:
    """Make routine text easier to tokenise: drop T-SQL table hints, and take the body of a PostgreSQL ``$$`` routine."""
    if dialect == "tsql":
        return _TSQL_OPTION.sub(" ", _TSQL_HINT.sub(" ", text))
    if dialect == "postgres":
        match = _PG_BODY.search(text)
        body = match.group(2) if match else text
        return _PG_PERFORM.sub("SELECT", body)  # PL/pgSQL `PERFORM <query>` is a SELECT whose result is discarded
    return text


def _candidates(toks: list[Any]) -> list[int]:
    depth, found = 0, []
    for i, tok in enumerate(toks):
        kind = tok.token_type
        if kind == TT.L_PAREN:
            depth += 1
        elif kind == TT.R_PAREN:
            depth = max(0, depth - 1)
        elif depth == 0:
            if kind in _START:
                if (toks[i - 1].token_type if i else None) not in _GLUE_PREV:
                    found.append(i)
            elif kind == TT.CREATE and i + 1 < len(toks) and toks[i + 1].text.upper() in _TABLE_WORDS:
                found.append(i)
    return found


def _cut(chunk: list[Any]) -> list[Any]:
    """End the chunk at the first depth-0 semicolon or block word. ``UPDATE .. SET`` and ``CASE .. END`` stay whole."""
    depth, case_depth, keep = 0, 0, []
    first = chunk[0].token_type if chunk else None
    for k, tok in enumerate(chunk):
        word = "" if tok.token_type in _QUOTED else tok.text.upper()
        if tok.token_type == TT.L_PAREN:
            depth += 1
        elif tok.token_type == TT.R_PAREN:
            depth = max(0, depth - 1)
        elif depth == 0 and tok.token_type == TT.CASE:
            case_depth += 1
        elif depth == 0 and case_depth and word == "END":
            case_depth -= 1
        elif (
            depth == 0
            and k > 0
            and not case_depth
            and (tok.token_type == TT.SEMICOLON or word in _STOP)
            and not (word == "SET" and first in (TT.UPDATE, TT.MERGE))
        ):
            break
        keep.append(tok)
    return keep


def split_statements(
    text: str,
    engine: str,
    *,
    deadline: float | None = None,
    max_statements: int = 2000,
) -> Split:
    """Tokenise one routine and parse each statement in it. ``deadline`` is a ``time.monotonic()`` value."""
    name = DIALECTS.get(engine)
    if name is None:
        return Split(error=f"no SQL dialect for engine {engine}")
    body = prepare_text(text, name)
    result = Split(dynamic=len(_DYNAMIC.findall(body)))
    try:
        dialect = Dialect.get_or_raise(name)
        toks = dialect.tokenize(body)
    except Exception as exc:  # noqa: BLE001 - an unterminated string etc. skips this routine only
        result.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        return result

    cands = _candidates(toks)
    parser = dialect.parser(error_level=ErrorLevel.RAISE)
    covered = -1
    for n, start in enumerate(cands):
        if n < covered:
            continue  # already inside a longer statement that parsed
        if result.found >= max_statements or (deadline is not None and time.monotonic() > deadline):
            result.truncated = True
            break
        result.found += 1
        for span in range(1, MAX_CHUNK_SPAN + 1):
            end = cands[n + span] if n + span < len(cands) else len(toks)
            chunk = _cut(toks[start:end])
            if not chunk:
                continue
            try:
                tree = parser.parse(chunk, body)[0]
            except Exception:  # noqa: BLE001, S112 - this chunk is not one statement; try a longer one
                continue
            if tree is None or isinstance(tree, exp.Command):
                continue
            result.trees.append(tree)
            result.parsed += 1
            covered = n + span
            break
    return result


__all__ = ["DIALECTS", "Split", "prepare_text", "split_statements"]
