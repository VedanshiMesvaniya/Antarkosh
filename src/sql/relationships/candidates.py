"""Candidates and edges (auto-schema Phase 6, steps 6c and 6d).

A detector (6c) says "this column may reference that column" and returns a :class:`Candidate`: one piece of
evidence with a rule code and a weight. The gates (6d) group the candidates of one column pair into an
:class:`Edge`, filter it, orient it and decide whether it is ambiguous. Step 6e scores and writes the edges.

Pure data and arithmetic. No I/O.

Origin: new (no previous file)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Starting weights. They are measured and re-tuned in step 6i (benchmark); nothing else holds a number.
WEIGHTS: dict[str, float] = {
    "declared": 1.00,  # step 4: a foreign key the database itself declares
    "proc_high": 0.90,  # step 5: join seen in 2+ routines
    "proc_low": 0.55,  # step 5: join seen once
    "table_id": 0.60,  # step 6: <table>_id -> that table's key
    "same_name": 0.55,  # step 7: same column name as a parent key
    "pk_name": 0.55,  # step 7: same, for a name found in many tables that is the sole primary key of one table
    "abbrev": 0.40,  # step 8: cust_id -> customer
    "role_prefix": 0.45,  # step 9: billing_address_id -> address
    "self_ref": 0.55,  # step 10: parent_id -> own key
    "self_ref_role": 0.40,  # step 10: manager_id -> own key (the table may not be a person table)
    "code_like": 0.45,  # step 11: po_number -> po.po_no (unique)
    "junction": 0.20,  # step 14: part of a composite key whose other part is also a reference
    "header_detail": 0.20,  # step 15: parent key + a line number
    "indexed": 0.05,  # step 18
    "comment": 0.15,  # step 19
}

# Rules that read the same fact from the same names. They are ONE witness, not several: customer_id ->
# customer.customer_id matches both "table id" and "same name", and must not count twice.
NAME_RULES = frozenset({"table_id", "same_name", "pk_name", "abbrev", "role_prefix", "self_ref", "code_like"})
STRUCTURE_RULES = frozenset({"junction", "header_detail"})


@dataclass(frozen=True)
class Candidate:
    """One piece of evidence that ``child.child_column`` references ``parent.parent_column``."""

    child: str  # qualified table name, as in CatalogGraph.tables
    child_column: str
    parent: str
    parent_column: str
    rule: str  # declared | proc | table_id | same_name | abbrev | role_prefix | self_ref | code_like | junction | header_detail
    weight: float
    symmetric: bool = False  # the detector could not tell which side is the parent (6d step 20 decides)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.child, self.child_column, self.parent, self.parent_column)


@dataclass
class Edge:
    """All the evidence for one directed reference, after grouping. Built by 6d, scored and written by 6e."""

    child: str
    child_column: str
    parent: str
    parent_column: str
    evidence: dict[str, float] = field(default_factory=dict)  # rule -> weight (max if a rule fired twice)
    symmetric_only: bool = False  # every piece of evidence left the direction open
    direction_known: bool = True  # False: nothing could tell parent from child; 6e must not send it to prompts
    cardinality: str = "many_to_one"  # many_to_one | one_to_one | shared_key
    ambiguous: bool = False  # the same column has more than one plausible parent

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.child, self.child_column, self.parent, self.parent_column)

    def add(self, rule: str, weight: float) -> None:
        if weight > self.evidence.get(rule, 0.0):
            self.evidence[rule] = weight

    def merge(self, other: Edge) -> None:
        for rule, weight in other.evidence.items():
            self.add(rule, weight)
        self.symmetric_only = self.symmetric_only and other.symmetric_only

    @property
    def has_hard_evidence(self) -> bool:
        """Declared by the database or seen in a stored procedure: facts, not guesses."""
        return "declared" in self.evidence or "proc" in self.evidence

    @property
    def score(self) -> float:
        return noisy_or(self.evidence)


def _category(rule: str) -> str:
    if rule in NAME_RULES:
        return "name"
    if rule in STRUCTURE_RULES:
        return "structure"
    return rule


def noisy_or(evidence: dict[str, float]) -> float:
    """Combine independent witnesses: 1 - prod(1 - w). Rules that read the same names count once (their best weight)."""
    best: dict[str, float] = {}
    for rule, weight in evidence.items():
        cat = _category(rule)
        best[cat] = max(best.get(cat, 0.0), weight)
    miss = 1.0
    for weight in best.values():
        miss *= 1.0 - min(max(weight, 0.0), 1.0)
    return round(1.0 - miss, 4)


__all__ = ["NAME_RULES", "STRUCTURE_RULES", "WEIGHTS", "Candidate", "Edge", "noisy_or"]
