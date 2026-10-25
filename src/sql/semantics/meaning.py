"""Column meaning cards (auto-schema Phase 9b): one batched LLM call per table, metadata only, then strict checking.

The model is asked, for each column of a table, for a full name, a one-line meaning, 3 to 6 phrases a user might say, the
evidence ids it used and a confidence. Everything it returns is treated as UNTRUSTED until checked:

* the column names it answers must be columns of that table, nothing else is kept;
* the expansion must FIT the name: every word of the name has to be accounted for by the expansion;
* cited evidence ids must exist; an invented citation caps the confidence;
* the confidence is capped by what is actually known: ``high`` needs real evidence (a comment, a key, a procedure that
  names it) or a name made only of ordinary words and listed abbreviations; a guess about an opaque name is never ``high``;
* the same column name must mean the same thing in every table (a cross-table pass lowers the odd ones out).

Only metadata reaches the model: names, types, comments, constants seen in procedure code. No data row, ever.

Origin: new (no previous file)
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.sql.relationships.names import normalized_column_name, singular
from src.sql.semantics.abbreviations import expansion_fits
from src.sql.semantics.evidence import ColumnEvidence, TableEvidence

PROMPT_VERSION = 1
MAX_COLUMNS_PER_CALL = 40
MAX_FULL_NAME_WORDS = 8
MAX_MEANING_CHARS = 160
MAX_PHRASES = 6
CONSISTENCY_FLAGS = ("conflicts_with_other_tables", "inconsistent_across_tables")
LLM = Callable[[list[dict[str, str]], int], Awaitable[str]]

SYSTEM_PROMPT = """You document database columns for a text-to-SQL system. You get the metadata of ONE table: its columns, their types, comments and numbered evidence items. For every column write what it holds in business terms.

Rules:
- Use ONLY the metadata given. Never invent facts about the business. If a name is opaque and no evidence explains it, answer meaning "unknown" and confidence "low".
- "full_name": the column's name as plain words, abbreviations expanded (po_no -> "purchase order number"). 1 to 6 words, lower case. Every word of the column name must be reflected in it.
- "meaning": one short sentence, at most 20 words.
- "phrases": 3 to 6 different ways a business user might refer to this column in a question, 2 to 5 words each, lower case. Never a column name, a table name or SQL.
- "evidence": the ids of the evidence items you relied on (e.g. ["comment", "param1"]); [] if you relied on the name alone.
- "confidence": "high" only if evidence or the name itself makes the meaning certain; "medium" for a reasonable reading of the name; "low" for a guess.
- Answer for every column listed, using its exact name.

Reply with JSON only, no prose and no markdown:
{"columns": [{"name": "...", "full_name": "...", "meaning": "...", "phrases": ["..."], "evidence": ["..."], "confidence": "high|medium|low"}]}"""


@dataclass(frozen=True)
class ColumnMeaning:
    name: str
    full_name: str
    meaning: str
    phrases: tuple[str, ...]
    evidence: tuple[str, ...]
    confidence: str
    flags: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name, "meaning": self.meaning, "phrases": list(self.phrases), "evidence": list(self.evidence),
            "confidence": self.confidence, "base_confidence": self.confidence, "flags": list(self.flags),
        }


# ---------------------------------------------------------------- the request


def sendable(columns: tuple[ColumnEvidence, ...]) -> list[ColumnEvidence]:
    """Columns worth a model's time: a name many tables share (id, created_at, company_id) says nothing specific to this table
    unless somebody wrote something about it."""
    return [c for c in columns if not c.common or c.hard]


def build_messages(table: TableEvidence, columns: list[ColumnEvidence]) -> list[dict[str, str]]:
    payload = {
        "table": table.table,
        "table_comment": table.comment,
        "all_columns_of_the_table": list(table.neighbors),
        "columns": [
            {
                "name": c.name,
                "type": c.type,
                "key": "primary key" if c.is_primary_key else ("foreign key" if c.is_foreign_key else ""),
                "evidence": [{"id": i.id, "kind": i.kind, "text": i.text} for i in c.items],
            }
            for c in columns
        ],
    }
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


# ---------------------------------------------------------------- the response


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_response(text: str | None) -> list[dict[str, Any]] | None:
    """The list of column answers, or None if the reply is not usable JSON of the expected shape."""
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip()).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    rows = data.get("columns") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


_SQLISH = re.compile(r"[_.()]|\b(select|from|where|join|group by|order by)\b", re.IGNORECASE)


def _clean_phrases(raw: Any) -> tuple[str, ...]:
    out: list[str] = []
    for p in raw if isinstance(raw, list) else []:
        text = " ".join(str(p).lower().split())
        words = text.split()
        if not 2 <= len(words) <= 5 or len(text) > 40 or _SQLISH.search(text) or text in out:
            continue
        out.append(text)
    return tuple(out[:MAX_PHRASES])


def finalize_column(raw: dict[str, Any], col: ColumnEvidence) -> ColumnMeaning:
    """Check one answer against what is known about the column and cap its confidence accordingly."""
    flags: list[str] = []
    full_name = " ".join(str(raw.get("full_name") or "").lower().split())
    meaning = " ".join(str(raw.get("meaning") or "").split())[:MAX_MEANING_CHARS]
    llm_conf = str(raw.get("confidence") or "").lower()
    confidence = llm_conf if llm_conf in ("high", "medium", "low") else "low"

    cited_raw = [str(e) for e in raw.get("evidence") or [] if isinstance(e, (str, int))] if isinstance(raw.get("evidence"), list) else []
    known = col.item_ids()
    cited = tuple(dict.fromkeys(e for e in cited_raw if e in known))
    if len(cited) != len(set(cited_raw)):
        flags.append("bad_citation")

    if not full_name or len(full_name.split()) > MAX_FULL_NAME_WORDS or meaning.lower() in ("", "unknown"):
        flags.append("unknown")
        confidence = "low"
    elif not expansion_fits(col.name, full_name):
        flags.append("expansion_mismatch")
        confidence = "low"
    if confidence == "high" and "bad_citation" in flags:
        confidence = "medium"
    if confidence == "high" and not (col.hard or col.explains_itself):
        flags.append("no_evidence")
        confidence = "medium"
    return ColumnMeaning(
        name=col.name, full_name=full_name, meaning=meaning, phrases=_clean_phrases(raw.get("phrases")),
        evidence=cited, confidence=confidence, flags=tuple(dict.fromkeys(flags)),
    )


def interpret(rows: list[dict[str, Any]], columns: list[ColumnEvidence]) -> dict[str, ColumnMeaning]:
    """Match answers to the columns that were asked about (case-insensitively). Anything else the model wrote is dropped."""
    wanted = {c.name.lower(): c for c in columns}
    out: dict[str, ColumnMeaning] = {}
    for row in rows:
        key = str(row.get("name") or "").lower()
        if key in wanted and key not in out:
            out[key] = finalize_column(row, wanted[key])
    return out


# ---------------------------------------------------------------- one table


@dataclass
class TableOutcome:
    columns: dict[str, ColumnMeaning]
    calls: int
    skipped_common: int
    missing: int  # columns the model did not answer


async def generate_table(table: TableEvidence, llm: LLM, *, max_columns: int = MAX_COLUMNS_PER_CALL) -> TableOutcome | None:
    """Meanings for one table. A wide table is split into several calls. None if any call stays unusable after one retry,
    so a table is stored whole or not at all."""
    todo = sendable(table.columns)
    skipped = len(table.columns) - len(todo)
    result: dict[str, ColumnMeaning] = {}
    calls = 0
    for start in range(0, len(todo), max_columns):
        chunk = todo[start : start + max_columns]
        messages = build_messages(table, chunk)
        budget = min(6000, 130 * len(chunk) + 300)
        rows = None
        for attempt in (1, 2):
            calls += 1
            try:
                rows = parse_response(await llm(messages if attempt == 1 else [*messages, {"role": "user", "content": "Reply with the JSON object only."}], budget))
            except Exception:  # noqa: BLE001 - a provider failure on this table is reported by the caller, never raised
                rows = None
                break
            if rows is not None:
                break
        if rows is None:
            return None
        result.update(interpret(rows, chunk))
    answered = {c.name.lower() for c in todo} & set(result)
    return TableOutcome(columns=result, calls=calls, skipped_common=skipped, missing=len(todo) - len(answered))


# ---------------------------------------------------------------- the same name must mean the same thing


_STOP = frozenset({"of", "the", "a", "an", "and", "or"})


def _words(full_name: str) -> frozenset[str]:
    return frozenset(singular(w) for w in re.findall(r"[a-z0-9]+", full_name.lower()) if w not in _STOP)


def _similar(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def apply_consistency(tables: dict[str, dict[str, dict[str, Any]]], common_names: frozenset[str]) -> int:
    """Lower the confidence of a column whose name means something different in another table. Idempotent: it always starts
    again from each column's own ``base_confidence``. Returns how many columns it changed."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for t, cols in tables.items():
        for c, e in cols.items():
            base = e.get("base_confidence", e.get("confidence", "low"))
            e["base_confidence"] = base
            e["confidence"] = base
            e["flags"] = [f for f in e.get("flags", []) if f not in CONSISTENCY_FLAGS]
            n = normalized_column_name(c)
            if base != "low" and n and n not in common_names and e.get("full_name"):
                groups.setdefault(n, []).append((t, c))
    changed = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        sets = {m: _words(tables[m[0]][m[1]]["full_name"]) for m in members}
        rep = Counter(sets.values()).most_common()
        top = max(n for _, n in rep)
        representative = min((s for s, n in rep if n == top), key=lambda s: sorted(s))
        agree = [m for m in members if _similar(sets[m], representative) >= 0.5]
        if len(agree) >= max(2, math.ceil(0.6 * len(members))):
            for m in members:
                if m in agree:
                    continue
                e = tables[m[0]][m[1]]
                if "comment" in e.get("evidence", []):  # somebody documented the difference: keep it
                    continue
                e["confidence"] = "low"
                e["flags"].append("conflicts_with_other_tables")
                changed += 1
        elif len(agree) < len(members):  # no clear majority: nobody is certain
            for m in members:
                e = tables[m[0]][m[1]]
                if e["confidence"] == "high":
                    e["confidence"] = "medium"
                    e["flags"].append("inconsistent_across_tables")
                    changed += 1
    return changed


__all__ = [
    "CONSISTENCY_FLAGS", "LLM", "MAX_COLUMNS_PER_CALL", "PROMPT_VERSION", "SYSTEM_PROMPT", "ColumnMeaning", "TableOutcome",
    "apply_consistency", "build_messages", "finalize_column", "generate_table", "interpret", "parse_response", "sendable",
]
