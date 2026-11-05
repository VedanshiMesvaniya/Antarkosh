"""Learning from usage (auto-schema Phase 14): real user phrasing -> schema element, learned slowly and reversibly.

People say "QN" or "vendor claim", not ``quality_notification`` or ``vendor_claims``. What they say is better evidence than
a guess from column names, but one accepted answer can still be a wrong answer. So nothing is learned from a single success:

1. **Evidence, not conclusions.** Every thumbs-up / thumbs-down on a SQL answer appends ONE line to
   ``databases/<db_id>/learned/term_pair_evidence.jsonl``: the question's phrases (never the full question), the tables and
   columns the SQL used, an hour-free day, a hashed user and a hashed question. Nothing else is stored.
2. **Promotion is a pure function of that ledger** (``evaluate``). A (phrase -> target) pair is promoted only after repeated,
   consistent confirmations: several DISTINCT questions, on more than one day, where the phrase went with that table almost
   every time (measured against EVERY confirmed answer that used the phrase), and with (almost) no thumbs-down. Because it is recomputed from the ledger, a withdrawn thumbs-up, a thumbs-down
   or an admin revoke simply removes the pair the next time it is computed: that is the undo.
3. **Separate home.** Everything lives in ``databases/<db_id>/learned/`` (git-ignored, outside the generated-file list that a
   sync writes, snapshots and rolls back). A sync never reads, rewrites or deletes it.
4. **Used only as a hint, only when switched on.** ``term_pairs.json`` is read by the prompt builder when the per-database flag
   ``auto_learning_apply`` is on (it starts off). A learned entry never replaces, reorders or overrides a curated or generated
   glossary entry, and it points at a table or column that must still exist in the current schema.

What is NOT learned (each is a deliberate guard, not an accident): words the schema or an existing glossary entry already
explains; very generic words; anything that looks like a data value (tokens that appear as literals in the SQL, numbers);
join-key columns and audit/common columns; phrases longer than three words; a phrase that goes with several tables equally well
(nothing is promoted). Only TABLES are promoted: from co-occurrence alone a column cannot be told apart from the table it lives in,
and a wrong column hint misleads more than a right table hint helps. Columns are still recorded, to know which words the schema's
own names already cover (a word satisfied by a column of the answer is never tied to some other column of the same query).

Origin: new (no previous file)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from src.sql.knowledge.loaders import get_learned_path, validate_db_id

logger = logging.getLogger(__name__)

LEDGER_FILE = "term_pair_evidence.jsonl"
PAIRS_FILE = "term_pairs.json"
LOCK_FILE = ".term_pairs.lock"
GENERATED_BY = "learned_term_pairs"
LEDGER_VERSION = 1

# ---- promotion thresholds (one place, on purpose) -------------------------------------------------------------------------
MIN_CONFIRMATIONS = 3  # distinct questions (same question asked again does not count twice)
MIN_DISTINCT_DAYS = 2  # a burst of testing on one afternoon is not usage
MIN_DISTINCT_USERS = 1  # raise to 2 once the deployment has several real users
MIN_SHARE = 0.8  # of the confirmed answers that used the phrase, this share also used the target
MAX_DOWN_SHARE = 0.2  # thumbs-down / (thumbs-up + thumbs-down) for the same pair
MAX_PROMOTED = 300
MAX_PENDING_LISTED = 50
MAX_PHRASES_PER_EVENT = 30
MAX_TARGETS_PER_EVENT = 24
MAX_PHRASE_WORDS = 3
MAX_LEDGER_EVENTS = 50_000  # past this, new evidence is dropped (and logged) instead of growing without bound
MAX_LEARNED_LINES_IN_PROMPT = 5

_WORD = re.compile(r"[a-z0-9]+")

# Words that never start or continue a business phrase. The first set mirrors the prompt builder's glossary stop words.
_STOP = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "of", "for", "in", "on", "at", "to", "by", "with", "and", "or", "our",
        "my", "your", "who", "what", "which", "show", "get", "list", "how", "much", "many", "this", "that", "from", "tell",
        "me", "all", "each", "every", "per", "please", "find", "display", "give", "between", "than", "more", "less", "where",
        "when", "whose", "their", "its", "has", "have", "had", "does", "do", "did", "be", "been", "not", "no", "any", "most",
        "top", "last", "first", "highest", "lowest", "average", "avg", "sum", "also", "can", "you", "i", "we", "it", "as",
        "if", "then", "there", "these", "those", "into", "over", "under", "about", "only", "total", "count", "number", "above",
        "below", "against", "greater", "equal", "least", "within", "during", "before", "after", "since", "until", "ago", "versus",
        "vs", "other", "another", "same", "both", "either", "just", "like", "using", "named", "called", "name",
    }
)
# Single words too generic to ever be a learned phrase (kept short; a multi-word phrase containing them is fine).
_GENERIC = frozenset(
    {
        "name", "date", "status", "type", "code", "id", "amount", "value", "quantity", "description", "time", "day", "month",
        "year", "record", "records", "data", "detail", "details", "item", "items", "information", "info", "report", "result",
        "results", "current", "active", "new", "old", "recent", "latest", "pending", "open", "closed", "week", "today",
        "amounts", "totals", "values", "counts", "numbers", "quantities", "sums", "rows", "entries", "list", "lists",
    }
)
# Columns that say nothing about meaning (same idea as the relationship detectors' seed list).
_COMMON_COLUMNS = frozenset(
    {
        "id", "name", "code", "status", "type", "description", "remarks", "remark", "notes", "note", "title", "comment",
        "comments", "created_at", "created_on", "created_date", "created_by", "updated_at", "updated_on", "updated_date",
        "updated_by", "deleted_at", "deleted_on", "deleted_by", "modified_at", "modified_on", "is_active", "active",
        "is_deleted", "deleted", "flag", "sort_order", "seq", "sequence", "version", "date", "value", "amount", "qty",
        "quantity",
    }
)


# ============================================================================================ small pure helpers


def _split_identifier(name: str) -> list[str]:
    from src.sql.relationships.names import split_identifier

    return split_identifier(name)


def _singular(token: str) -> str:
    from src.sql.relationships.names import singular

    return singular(token)


def _normalized_column(name: str) -> str:
    return "_".join(_split_identifier(name))


def normalize_question(question: str) -> list[str]:
    return _WORD.findall((question or "").lower())


def question_hash(question: str) -> str:
    return hashlib.sha256(" ".join(normalize_question(question)).encode("utf-8")).hexdigest()[:16]


def user_hash(db_id: str, user_id: str | None) -> str:
    return hashlib.sha256(f"{db_id}:{user_id or ''}".encode()).hexdigest()[:12]


def _stems(tokens: list[str]) -> set[str]:
    return {_singular(t) for t in tokens}


def target_name_tokens(target: str) -> list[str]:
    """Words of a target's own name: ``column:dbo.qn_header.qn_no`` -> qn, header, qn, no (the schema part is ignored)."""
    body = target.split(":", 1)[-1]
    parts = body.split(".")
    if target.startswith("column:"):
        names = [parts[-2] if len(parts) >= 2 else "", parts[-1]]
    else:
        names = [parts[-1]]
    out: list[str] = []
    for n in names:
        out.extend(_split_identifier(n))
    return out


def explained_by_name(phrase: str, targets: str | list[str] | tuple[str, ...] | set[str]) -> bool:
    """The schema's own names already account for every word of the phrase: there is nothing to learn from this answer.

    ``targets`` is one target or all the targets of one answer. Counting all of them matters: if the answer used a column called
    ``rating``, the word "rating" was satisfied by that column and must not be tied to some other column of the same query.
    """
    words = phrase.split()
    if not words:
        return True
    names = [targets] if isinstance(targets, str) else list(targets)
    covered: set[str] = set()
    for t in names:
        covered |= _stems(target_name_tokens(t))
    return _stems(words) <= covered


# ============================================================================================ schema vocabulary


class SchemaVocab:
    """Tables and columns of one database, from ``schema/schema.json`` (read only). Case-insensitive lookups."""

    def __init__(self, tables: dict[str, tuple[str, dict[str, str]]]) -> None:
        # canonical lower -> (canonical display, {column lower: column display})
        self._tables = tables
        self._bare: dict[str, list[str]] = defaultdict(list)
        for key in tables:
            self._bare[key.rsplit(".", 1)[-1]].append(key)

    @classmethod
    def from_document(cls, doc: Any) -> SchemaVocab:
        tables: dict[str, tuple[str, dict[str, str]]] = {}
        entries = doc.get("tables") if isinstance(doc, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            display = str(entry.get("qualified_name") or entry["name"])
            cols = {str(c["name"]).lower(): str(c["name"]) for c in entry.get("columns") or [] if isinstance(c, dict) and c.get("name")}
            tables[display.lower()] = (display, cols)
        return cls(tables)

    @classmethod
    def load(cls, db_id: str, databases_dir: Path | None = None) -> SchemaVocab:
        from src.sql import registry

        base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
        path = base / db_id / "schema" / "schema.json"
        try:
            return cls.from_document(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls({})

    def __bool__(self) -> bool:
        return bool(self._tables)

    def resolve_table(self, name: str, schema: str | None = None) -> str | None:
        n = (name or "").lower()
        if not n:
            return None
        if schema:
            hit = self._tables.get(f"{schema.lower()}.{n}")
            if hit:
                return hit[0]
        hit = self._tables.get(n)
        if hit:
            return hit[0]
        owners = self._bare.get(n, [])
        return self._tables[owners[0]][0] if len(owners) == 1 else None

    def column(self, table: str, column: str) -> str | None:
        entry = self._tables.get(table.lower())
        return entry[1].get((column or "").lower()) if entry else None

    def has_target(self, target: str) -> bool:
        kind, _, body = target.partition(":")
        if kind == "table":
            return body.lower() in self._tables
        if kind == "column":
            table, _, col = body.rpartition(".")
            return bool(table) and self.column(table, col) is not None
        return False


# ============================================================================================ question -> phrases


def sql_literal_tokens(tree: Any) -> set[str]:
    """Words that appear as literal values in the SQL: these are data, not vocabulary."""
    from sqlglot import exp

    out: set[str] = set()
    for lit in tree.find_all(exp.Literal):
        out.update(_WORD.findall(str(lit.this).lower()))
    return out


def extract_phrases(question: str, *, value_tokens: set[str] | frozenset[str] = frozenset(), explained: Any = None) -> list[str]:
    """1-3 word phrases of consecutive meaningful words of the question.

    ``value_tokens`` are words that are data values in the SQL; they break a phrase and are never part of one. ``explained`` is an
    optional callable ``phrase -> bool`` (an existing glossary entry already covers it).
    """
    runs: list[list[str]] = [[]]
    for tok in normalize_question(question):
        if tok in _STOP or tok.isdigit() or len(tok) < 2 or tok in value_tokens:
            if runs[-1]:
                runs.append([])
            continue
        runs[-1].append(tok)
    seen: dict[str, None] = {}
    for run in runs:
        for n in range(1, MAX_PHRASE_WORDS + 1):
            for i in range(len(run) - n + 1):
                words = run[i : i + n]
                if n == 1 and words[0] in _GENERIC:
                    continue
                if all(w in _GENERIC for w in words):
                    continue
                phrase = " ".join(words)
                if explained is not None and explained(phrase):
                    continue
                seen.setdefault(phrase, None)
    return list(seen)[:MAX_PHRASES_PER_EVENT]


# ============================================================================================ sql -> targets


def parse_sql(sql: str, dialect: str | None) -> Any | None:
    try:
        import sqlglot

        return sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001 - an unparsable statement is simply not learned from
        return None


def extract_targets(tree: Any, vocab: SchemaVocab) -> list[str]:
    """Real tables of the query, and the real columns it reads or filters on (join keys and common columns are left out)."""
    from sqlglot import exp

    if tree is None or not vocab:
        return []
    alias_to_table: dict[str, str] = {}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        canon = vocab.resolve_table(t.name, t.db or None)
        if not canon:
            continue
        if canon not in tables:
            tables.append(canon)
        alias_to_table[(t.alias or t.name).lower()] = canon
        alias_to_table.setdefault(t.name.lower(), canon)

    join_cols: set[int] = set()  # columns that only join tables say nothing about meaning
    for j in tree.find_all(exp.Join):
        on = j.args.get("on")
        if on is not None:
            join_cols.update(id(node) for node in on.find_all(exp.Column))

    columns: list[str] = []
    for c in tree.find_all(exp.Column):
        if id(c) in join_cols or not c.name:
            continue
        if _normalized_column(c.name) in _COMMON_COLUMNS:
            continue
        if c.table:
            canon = alias_to_table.get(c.table.lower())
        else:
            owners = [t for t in tables if vocab.column(t, c.name)]
            canon = owners[0] if len(owners) == 1 else None
        if not canon:
            continue
        real = vocab.column(canon, c.name)
        if real:
            target = f"column:{canon}.{real}"
            if target not in columns:
                columns.append(target)
    targets = sorted(f"table:{t}" for t in tables) + sorted(columns)
    return targets[:MAX_TARGETS_PER_EVENT]


# ============================================================================================ ledger (append-only)


def _ledger_path(db_id: str, *, create: bool) -> Path:
    return get_learned_path(LEDGER_FILE, db_id=db_id, create_dir=create)


def _pairs_path(db_id: str, *, create: bool) -> Path:
    return get_learned_path(PAIRS_FILE, db_id=db_id, create_dir=create)


def read_ledger(db_id: str) -> list[dict[str, Any]]:
    """All well-formed events, oldest first. A missing file or a damaged line is skipped, never an error."""
    path = _ledger_path(db_id, create=False)
    events: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("kind") in {"up", "down", "withdraw", "revoke", "restore"}:
                    events.append(ev)
    except OSError:
        return []
    return events


def _append(db_id: str, event: dict[str, Any]) -> None:
    path = _ledger_path(db_id, create=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _now() -> datetime:
    return datetime.now(UTC)


# ============================================================================================ evaluation (pure)


def _final_states(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], bool]]:
    """Latest rating per message (a withdraw cancels it), and the revoke state per (phrase, target-or-*)."""
    last: dict[str, dict[str, Any]] = {}
    revoked: dict[tuple[str, str], bool] = {}
    for ev in events:
        kind = ev["kind"]
        if kind in {"up", "down", "withdraw"}:
            msg = str(ev.get("msg") or "")
            if msg:
                last[msg] = ev
        else:
            key = (str(ev.get("phrase") or "").lower(), str(ev.get("target") or "*"))
            if key[0]:
                revoked[key] = kind == "revoke"
    ups: list[dict[str, Any]] = []
    seen_q: set[str] = set()
    downs: list[dict[str, Any]] = []
    for ev in last.values():  # dict keeps first-seen order of messages
        if ev["kind"] == "up":
            q = str(ev.get("q") or "")
            if q and q in seen_q:
                continue  # the same question confirmed twice is one confirmation
            seen_q.add(q)
            ups.append(ev)
        elif ev["kind"] == "down":
            downs.append(ev)
    return ups, downs, revoked


def evaluate(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(promoted, pending)`` from the ledger alone. Deterministic: same ledger, same answer."""
    ups, downs, revoked = _final_states(events)
    revoked_keys = {k for k, v in revoked.items() if v}

    phrase_total: Counter[str] = Counter()  # every confirmed answer whose question used the phrase
    pair_ups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ev in ups:
        targets = {str(t) for t in ev.get("targets") or []}
        for p in {str(p) for p in ev.get("phrases") or []}:
            phrase_total[p] += 1
            if explained_by_name(p, targets):
                continue  # the schema's own names already cover this phrase in this answer: it is not evidence
            for t in targets:
                if t.startswith("table:"):
                    pair_ups[(p, t)].append(ev)
    pair_downs: Counter[tuple[str, str]] = Counter()
    for ev in downs:
        targets = {str(t) for t in ev.get("targets") or []}
        for p in {str(p) for p in ev.get("phrases") or []}:
            if explained_by_name(p, targets):
                continue
            for t in targets:
                if t.startswith("table:"):
                    pair_downs[(p, t)] += 1

    def stats(p: str, t: str) -> dict[str, Any]:
        evs = pair_ups[(p, t)]
        n, d = len(evs), pair_downs[(p, t)]
        return {
            "phrase": p,
            "target": t,
            "confirmations": n,
            "days": len({str(e.get("ts", ""))[:10] for e in evs}),
            "users": len({e.get("user") for e in evs}),
            "share": round(n / phrase_total[p], 3) if phrase_total[p] else 0.0,
            "downs": d,
            "down_share": round(d / (n + d), 3) if (n + d) else 0.0,
            "first_seen": min((str(e.get("ts", "")) for e in evs), default=""),
        }

    def passes(s: dict[str, Any]) -> bool:
        return (
            s["confirmations"] >= MIN_CONFIRMATIONS
            and s["days"] >= MIN_DISTINCT_DAYS
            and s["users"] >= MIN_DISTINCT_USERS
            and s["share"] >= MIN_SHARE
            and s["down_share"] <= MAX_DOWN_SHARE
        )

    by_phrase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (p, t) in pair_ups:
        if (p, "*") in revoked_keys or (p, t) in revoked_keys:
            continue
        by_phrase[p].append(stats(p, t))

    promoted: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for p in sorted(by_phrase):
        rows = by_phrase[p]
        winners = [s for s in rows if passes(s)]
        if len(winners) == 1:  # two tables that both pass (a join companion, a twin) is ambiguous: nothing is promoted
            promoted.append({**winners[0], "kind": "table"})
        else:
            best = max(rows, key=lambda s: (s["confirmations"], s["share"], s["target"]))
            pending.append(best)
    promoted.sort(key=lambda s: (-s["confirmations"], s["phrase"]))
    pending.sort(key=lambda s: (-s["confirmations"], -s["share"], s["phrase"]))
    return promoted[:MAX_PROMOTED], pending[:MAX_PENDING_LISTED]


def thresholds() -> dict[str, Any]:
    return {
        "min_confirmations": MIN_CONFIRMATIONS,
        "min_distinct_days": MIN_DISTINCT_DAYS,
        "min_distinct_users": MIN_DISTINCT_USERS,
        "min_share": MIN_SHARE,
        "max_down_share": MAX_DOWN_SHARE,
    }


# ============================================================================================ derived file


def _content_key(promoted: list[dict[str, Any]], pending: list[dict[str, Any]], limits: Any) -> str:
    keep = ("phrase", "target", "confirmations", "days", "users", "share", "downs", "down_share")

    def slim(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: r.get(k) for k in keep} for r in rows if isinstance(r, dict)]

    return json.dumps({"p": slim(promoted), "n": slim(pending), "t": limits}, sort_keys=True)


def recompute(db_id: str) -> dict[str, Any]:
    """Rebuild ``learned/term_pairs.json`` from the ledger. Returns ``{"written", "promoted", "pending"}``. Never raises."""
    out: dict[str, Any] = {"written": False, "promoted": 0, "pending": 0}
    try:
        validate_db_id(db_id)
        promoted, pending = evaluate(read_ledger(db_id))
        out["promoted"], out["pending"] = len(promoted), len(pending)
        path = _pairs_path(db_id, create=False)
        previous: dict[str, Any] = {}
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        old_promoted_at = {(e.get("phrase"), e.get("target")): e.get("promoted_at") for e in previous.get("promoted") or [] if isinstance(e, dict)}
        stamp = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        for s in promoted:
            s["promoted_at"] = old_promoted_at.get((s["phrase"], s["target"])) or stamp
        old_key = None
        if isinstance(previous, dict) and previous.get("generated_by") == GENERATED_BY:
            old_key = _content_key(previous.get("promoted") or [], previous.get("pending") or [], previous.get("thresholds"))
        if old_key == _content_key(promoted, pending, thresholds()):
            return out
        if not promoted and not pending and not path.exists():
            return out
        from src.sql import registry

        path = _pairs_path(db_id, create=True)
        registry.atomic_write_json(
            path,
            {
                "generated_by": GENERATED_BY,
                "version": 1,
                "generated_at": stamp,
                "thresholds": thresholds(),
                "promoted": promoted,
                "pending": pending,
            },
            mode=0o644,
        )
        out["written"] = True
    except Exception as exc:  # noqa: BLE001 - learning must never break the caller
        logger.warning("term-pair recompute failed for %s: %s", db_id, exc)
    return out


# ============================================================================================ recording


class _Lock:
    def __init__(self, db_id: str) -> None:
        self._path = get_learned_path(LOCK_FILE, db_id=db_id, create_dir=True)
        self._fh: Any = None
        self._cm: Any = None

    def __enter__(self) -> Self:
        from src.core.file_lock import LockMode, locked

        self._fh = open(self._path, "a", encoding="utf-8")
        self._cm = locked(self._fh, LockMode.EXCLUSIVE)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._cm.__exit__(*exc)
        finally:
            self._fh.close()


def _glossary_explainer(db_id: str):
    """``phrase -> bool``: does an existing (curated or generated) column-glossary entry already match this phrase?"""
    try:
        from src.sql.prompt_builder import (
            GLOSSARY_STOP_WORDS,
            get_raw_column_glossary,
            matches_glossary_candidate,
            stem_word,
        )
    except Exception:  # noqa: BLE001
        return None
    data = get_raw_column_glossary(db_id) or {}
    cands: list[str] = []
    for term, details in data.items():
        if isinstance(details, dict):
            cands.append(term)
            cands.extend(s for s in details.get("synonyms", []) if isinstance(s, str))
    if not cands:
        return None

    def explained(phrase: str) -> bool:
        words = set(phrase.split()) - GLOSSARY_STOP_WORDS
        stems = {stem_word(w) for w in words}
        return any(matches_glossary_candidate(c, phrase, words, stems) for c in cands)

    return explained


def record_feedback(
    db_id: str,
    *,
    message_id: str,
    user_id: str | None,
    question: str,
    sql: str,
    row_count: int | None,
    signal: str | None,
    dialect: str | None = None,
    databases_dir: Path | None = None,
) -> dict[str, Any]:
    """Record one rating. ``signal`` is ``"up"``, ``"down"`` or ``None`` (rating removed). Never raises.

    Returns ``{"recorded": kind | None, "reason": str | None, ...recompute info}``.
    """
    result: dict[str, Any] = {"recorded": None, "reason": None}
    try:
        validate_db_id(db_id)
        if not message_id:
            result["reason"] = "no message id"
            return result
        ledger = read_ledger(db_id)
        if len(ledger) >= MAX_LEDGER_EVENTS:
            logger.warning("term-pair ledger for %s is full (%d events); new evidence is ignored", db_id, len(ledger))
            result["reason"] = "ledger full"
            return result
        current = None
        for ev in ledger:
            if ev["kind"] in {"up", "down", "withdraw"} and ev.get("msg") == message_id:
                current = ev["kind"]
        wanted = signal if signal in {"up", "down"} else "withdraw"
        if wanted == "withdraw" and current in (None, "withdraw"):
            result["reason"] = "nothing to withdraw"
            return result
        if wanted == current:
            result["reason"] = "already recorded"
            return result

        event: dict[str, Any] = {
            "v": LEDGER_VERSION,
            "ts": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": wanted,
            "msg": message_id,
            "user": user_hash(db_id, user_id),
        }
        if wanted != "withdraw":
            if not (sql or "").strip():
                result["reason"] = "no SQL on this answer"
                return result
            if wanted == "up" and not (row_count and row_count > 0):
                result["reason"] = "answer returned no rows"  # an empty answer confirms nothing
                return result
            vocab = SchemaVocab.load(db_id, databases_dir)
            tree = parse_sql(sql, dialect)
            if tree is None or not vocab:
                result["reason"] = "SQL or schema not readable"
                return result
            targets = extract_targets(tree, vocab)
            phrases = extract_phrases(question, value_tokens=sql_literal_tokens(tree), explained=_glossary_explainer(db_id))
            if not targets or not phrases:
                result["reason"] = "no phrases or no targets to learn from"
                return result
            event.update({"q": question_hash(question), "phrases": phrases, "targets": targets})

        with _Lock(db_id):
            _append(db_id, event)
            result.update(recompute(db_id))
        result["recorded"] = wanted
    except Exception as exc:  # noqa: BLE001
        logger.warning("term-pair recording failed for %s: %s", db_id, exc)
        result["reason"] = "error"
    return result


def set_revoked(db_id: str, phrase: str, target: str | None = None, *, revoke: bool = True) -> dict[str, Any]:
    """Admin undo. ``target=None`` blocks the phrase for every target. ``revoke=False`` lifts an earlier revoke."""
    validate_db_id(db_id)
    phrase = " ".join(normalize_question(phrase))
    if not phrase:
        raise ValueError("phrase is empty")
    event = {
        "v": LEDGER_VERSION,
        "ts": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "revoke" if revoke else "restore",
        "phrase": phrase,
        "target": target or "*",
    }
    with _Lock(db_id):
        _append(db_id, event)
        return recompute(db_id)


# ============================================================================================ reading (prompt side)

_READ_CACHE: dict[str, tuple[tuple[float, float], list[dict[str, str]]]] = {}


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_promoted(db_id: str, databases_dir: Path | None = None) -> list[dict[str, str]]:
    """Promoted pairs whose target still exists in the current schema: ``[{"phrase", "target", "maps_to"}]``. ``[]`` on any problem."""
    try:
        validate_db_id(db_id)
        from src.sql import registry

        pairs_path = _pairs_path(db_id, create=False)
        base = Path(databases_dir) if databases_dir is not None else registry.DATABASES_DIR
        schema_path = base / db_id / "schema" / "schema.json"
        key = (_mtime(pairs_path), _mtime(schema_path))
        cached = _READ_CACHE.get(f"{base}:{db_id}")
        if cached and cached[0] == key:
            return cached[1]
        out: list[dict[str, str]] = []
        if key[0]:
            doc = json.loads(pairs_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("generated_by") == GENERATED_BY:
                vocab = SchemaVocab.load(db_id, databases_dir)
                for e in doc.get("promoted") or []:
                    if isinstance(e, dict) and e.get("phrase") and e.get("target") and vocab.has_target(str(e["target"])):
                        out.append({"phrase": str(e["phrase"]), "target": str(e["target"]), "maps_to": str(e["target"]).split(":", 1)[1]})
        _READ_CACHE[f"{base}:{db_id}"] = (key, out)
        return out
    except Exception:  # noqa: BLE001 - the prompt must never fail because of this optional hint
        return []


def clear_read_cache() -> None:
    _READ_CACHE.clear()


def learned_glossary_lines(query: str, existing_text: str, db_id: str, databases_dir: Path | None = None) -> list[str]:
    """Prompt lines (same shape as the column glossary) for learned phrases found in ``query``. Curated entries always win.

    A learned line is skipped when the phrase already appears in the existing glossary text, or when the existing text already
    maps something to the same target. At most ``MAX_LEARNED_LINES_IN_PROMPT`` lines are added.
    """
    try:
        pairs = load_promoted(db_id, databases_dir)
        if not pairs:
            return []
        from src.sql.prompt_builder import (
            GLOSSARY_STOP_WORDS,
            matches_glossary_candidate,
            stem_word,
        )

        query_lower = (query or "").lower()
        words = set(re.findall(r"\w+", query_lower)) - GLOSSARY_STOP_WORDS
        stems = {stem_word(w) for w in words}
        existing = existing_text.lower()
        lines: list[str] = []
        for p in pairs:
            if len(lines) >= MAX_LEARNED_LINES_IN_PROMPT:
                break
            if not matches_glossary_candidate(p["phrase"], query_lower, words, stems):
                continue
            if f'"{p["phrase"]}"' in existing or f"→ {p['maps_to'].lower()}" in existing:
                continue
            lines.append(f'- "{p["phrase"]}" → {p["maps_to"]} (learned from confirmed answers)')
        return lines
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "GENERATED_BY", "LEDGER_FILE", "PAIRS_FILE", "SchemaVocab", "clear_read_cache", "evaluate", "explained_by_name",
    "extract_phrases", "extract_targets", "learned_glossary_lines", "load_promoted", "read_ledger", "recompute",
    "record_feedback", "set_revoked",
]
