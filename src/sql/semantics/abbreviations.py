"""A generic abbreviation list and the "does this expansion fit this name?" check (auto-schema Phase 9a/9b).

The list is deliberately small and generic: words that mean the same in almost every business database (``qty`` is
quantity, ``dt`` is date). Anything database-specific (``apq``, ``moq``) is NOT here; it has to be earned from a comment,
a procedure or the model's reading of the names, and gets a lower confidence for it.

The fit check is the guard against a model that writes a fluent but unrelated expansion: every token of the column name
must be accounted for by a word of the expansion (the word itself, a prefix, a short abbreviation of it, the initials of
consecutive words, or a listed abbreviation).

Origin: new (no previous file)
"""

from __future__ import annotations

import re

from src.sql.relationships.names import singular, split_identifier

# token -> the words it commonly stands for. More than one entry means the token is ambiguous (``inv``).
ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "no": ("number",), "num": ("number",), "nbr": ("number",), "nos": ("numbers",), "nm": ("name",), "nme": ("name",),
    "cnt": ("count",), "prc": ("price",), "chg": ("charge",),
    "qty": ("quantity",), "qnty": ("quantity",), "amt": ("amount",), "amnt": ("amount",),
    "dt": ("date",), "dte": ("date",), "ts": ("timestamp",), "tm": ("time",),
    "cd": ("code",), "desc": ("description",), "descr": ("description",), "dsc": ("description",),
    "addr": ("address",), "adr": ("address",), "tel": ("telephone",), "ph": ("phone",), "mob": ("mobile",),
    "cust": ("customer",), "cst": ("customer",), "vend": ("vendor",), "supp": ("supplier",), "emp": ("employee",),
    "usr": ("user",), "mgr": ("manager",), "dept": ("department",), "org": ("organization",), "cmp": ("company",),
    "po": ("purchase order",), "so": ("sales order",), "grn": ("goods receipt note",), "inv": ("invoice", "inventory"),
    "prod": ("product", "production"), "mat": ("material",), "itm": ("item",), "cat": ("category",),
    "pct": ("percent",), "perc": ("percent",), "yr": ("year",), "mth": ("month",), "wk": ("week",), "fy": ("fiscal year",),
    "ref": ("reference",), "seq": ("sequence",), "val": ("value",), "grp": ("group",), "src": ("source",),
    "tgt": ("target",), "dest": ("destination",), "pwd": ("password",), "acct": ("account",), "bal": ("balance",),
    "curr": ("currency",), "flg": ("flag",), "ind": ("indicator",), "sts": ("status",), "stat": ("status",),
    "typ": ("type",), "id": ("identifier",), "uom": ("unit of measure",), "sku": ("stock keeping unit",),
    "gst": ("goods and services tax",), "vat": ("value added tax",), "tds": ("tax deducted at source",),
    "pan": ("permanent account number",), "dob": ("date of birth",), "doj": ("date of joining",), "min": ("minimum",),
    "max": ("maximum",), "avg": ("average",), "tot": ("total",), "ttl": ("total",), "rem": ("remark", "remaining"),
    "rmk": ("remark",), "disc": ("discount",), "pmt": ("payment",), "pay": ("payment",), "ord": ("order",),
    "rec": ("record", "receipt"), "lvl": ("level",), "pri": ("priority",), "len": ("length",), "wt": ("weight",),
    "ht": ("height",), "dim": ("dimension",), "mfg": ("manufacturing",), "exp": ("expiry",), "bat": ("batch",),
    "lot": ("lot",), "loc": ("location",), "wh": ("warehouse",), "frm": ("from",), "upd": ("updated",), "crt": ("created",),
}

# Ordinary words that need no expansion: a name made only of these (and listed abbreviations) explains itself.
COMMON_WORDS = frozenset(
    ["name", "title", "date", "time", "year", "month", "week", "day", "status", "type", "code", "number", "amount", "total", "price", "cost", "rate", "value", "quantity", "count", "customer", "client", "vendor", "supplier", "employee", "user", "party", "person", "company", "organization", "department", "branch", "location", "address", "city", "state", "country", "region", "zone", "area", "pin", "zip", "email", "phone", "mobile", "fax", "website", "order", "sales", "purchase", "invoice", "receipt", "payment", "product", "item", "material", "stock", "batch", "lot", "category", "group", "class", "level", "priority", "reference", "source", "target", "description", "remark", "note", "comment", "text", "flag", "active", "enabled", "deleted", "created", "updated", "modified", "by", "at", "on", "from", "to", "start", "end", "begin", "finish", "due", "open", "closed", "approved", "pending", "rejected", "first", "last", "full", "middle", "sub", "main", "parent", "child", "unit", "weight", "height", "width", "length", "size", "color", "currency", "tax", "discount", "balance", "credit", "debit", "account", "bank", "cash", "contact", "designation", "role", "permission", "login", "password", "token", "session", "version", "sequence", "index", "sort", "rank", "score", "is", "has", "no", "yes", "id", "key", "hash", "id", "label", "tag", "remarks", "gender", "age", "birth", "joining", "expiry", "validity", "shift", "plan", "actual", "budget", "target", "limit", "min", "max", "average", "net", "gross", "grand", "head", "detail", "line", "row", "header", "footer", "page", "section"]
)

_WORD = re.compile(r"[a-z0-9]+")


def _is_abbrev(token: str, word: str) -> bool:
    """``qty`` -> quantity: same first letter, and the token's letters appear in order inside the word (3+ letters only)."""
    if len(token) < 3 or not word or token[0] != word[0] or len(token) > len(word):
        return False
    pos = 0
    for ch in word:
        if pos < len(token) and ch == token[pos]:
            pos += 1
    return pos == len(token)


def _token_fits(token: str, words: list[str], initials: str) -> bool:
    if token in words or singular(token) in {singular(w) for w in words}:
        return True
    if len(token) == 1:
        return token in initials
    if any(w.startswith(token) for w in words):
        return True
    if any(_is_abbrev(token, w) for w in words):
        return True
    if len(token) >= 2 and token in initials:  # po -> "purchase order", no -> ... in initials of consecutive words
        return True
    return any(all(part in words for part in e.split()) for e in ABBREVIATIONS.get(token, ()))


def expansion_fits(name: str, expansion: str) -> bool:
    """True if every word-like token of ``name`` is explained by ``expansion`` (extra words in the expansion are fine)."""
    tokens = [t for t in split_identifier(name) if not t.isdigit()]
    words = _WORD.findall((expansion or "").lower())
    if not tokens or not words:
        return False
    initials = "".join(w[0] for w in words)
    return all(_token_fits(t, words, initials) for t in tokens)


def self_explanatory(name: str) -> bool:
    """True if every token of ``name`` is an ordinary word or a listed abbreviation (``po_no``, ``customer_name``)."""
    tokens = [t for t in split_identifier(name) if not t.isdigit()]
    return bool(tokens) and all(t in COMMON_WORDS or t in ABBREVIATIONS or singular(t) in COMMON_WORDS for t in tokens)


def abbreviation_notes(name: str) -> list[str]:
    """``po_no`` -> ["po = purchase order", "no = number"]: the listed abbreviations that appear in the name.
    ``id`` is left out: it is understood everywhere and would only add noise to every key column."""
    return [f"{t} = {' or '.join(ABBREVIATIONS[t])}" for t in split_identifier(name) if t in ABBREVIATIONS and t != "id"]


__all__ = ["ABBREVIATIONS", "COMMON_WORDS", "abbreviation_notes", "expansion_fits", "self_explanatory"]
