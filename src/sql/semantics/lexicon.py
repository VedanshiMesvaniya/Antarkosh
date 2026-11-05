"""A small generic business lexicon: words that people use interchangeably in almost every business database.

Nothing here is specific to one database or one industry, and nothing here is used on its own: a group is only ever used when
exactly ONE of its words appears in the names of the database's own tables or columns (see ``routing.build_hints``). Then the
other words of the group are what people may say instead ("supplier" for a ``vendors`` table).

Rules for editing this list, because a wrong entry misleads every database that has the word:

* single lower-case words only; a word belongs to ONE group;
* only words that mean the same thing in nearly every business ("client" is a customer); no word with a second common meaning
  (``claim``, ``lead`` as a verb, ``order`` as a verb, ``item``, ``group``, ``class``, ``record``);
* a word shorter than four letters can never become a routing keyword (it would match inside other words), but may sit here.

Origin: new (no previous file)
"""

from __future__ import annotations

# canonical word first (the one most often used in table names), then the words that mean the same
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("customer", "client", "buyer", "consumer", "patron", "purchaser"),
    ("vendor", "supplier", "seller", "provider", "merchant"),
    ("employee", "staff", "worker", "personnel", "associate"),
    ("invoice", "bill"),
    ("payment", "remittance", "settlement"),
    ("product", "merchandise", "goods", "article"),
    ("inventory", "stock"),
    ("warehouse", "depot", "godown", "storehouse"),
    ("shipment", "consignment", "dispatch"),
    ("salary", "wage", "compensation", "remuneration"),
    ("expense", "expenditure", "spending", "outlay"),
    ("company", "organization", "firm", "enterprise"),
    ("department", "division"),
    ("manager", "supervisor"),
    ("complaint", "grievance"),
    ("appointment", "booking", "reservation"),
    ("student", "pupil", "learner"),
    ("teacher", "instructor", "tutor", "lecturer"),
    ("doctor", "physician"),
    ("tax", "levy", "duty"),
    ("discount", "rebate", "markdown"),
    ("refund", "reimbursement"),
    ("quotation", "quote", "estimate"),
    ("contract", "agreement"),
    ("machine", "equipment"),
    ("prospect", "enquiry", "inquiry"),
)

__all__ = ["SYNONYM_GROUPS"]
