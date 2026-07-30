"""Parser for SEC Form 4 ownership documents (insider transactions).

Turns one filing's XML into `Form4Transaction` rows. Reading the codes is
delegated to `wbj.filings.codes`, which is where the judgement lives; this
module's job is to extract faithfully and refuse to guess.

Three things this parser deliberately does NOT do:

- It does not treat `nonDerivativeHolding` entries as transactions. Those
  rows report a standing position with no transaction attached; counting
  them would invent activity that never happened.
- It does not infer a side from the acquired/disposed flag alone. `D` on a
  gift is not a sale (see `codes`).
- It does not drop rows it cannot classify. An unrecognized code surfaces
  as such, because a silently dropped filing looks identical to a company
  whose insiders did nothing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from wbj.filings.codes import CodeSpec, spec_for

# Filings from some agents carry a default namespace; most do not. Tag
# names are compared bare so both parse identically.
_NS = re.compile(r"^\{[^}]*\}")

# A 10b5-1 sale is scheduled months ahead by a plan adopted when the
# insider had no material information, so it carries no signal about what
# they know today. The document-level `aff10b5One` flag is the reliable
# marker; older filings only say so in a footnote, so both are checked.
_10B5_1_TEXT = re.compile(r"10b5-1", re.IGNORECASE)


@dataclass(frozen=True)
class Form4Transaction:
    """One reported transaction from a Form 4 filing."""

    issuer_name: str
    issuer_symbol: str
    owner_name: str
    owner_title: str
    date: str
    code: str
    spec: CodeSpec
    shares: float | None
    price: float | None
    acquired: bool
    security: str
    is_derivative: bool
    is_10b5_1: bool
    accession: str = ""
    filing_date: str = ""
    footnotes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def value_usd(self) -> float | None:
        """Dollar value of the transaction, or None if it cannot be known.

        A gift or an award reports a $0.00 price because no money changed
        hands — that is a genuine zero, not a missing value, and it stays
        0.0 so the $1M filter drops it. A missing price (some derivative
        rows omit it) returns None instead, so a value that was never
        reported is never mistaken for a transaction worth nothing.
        """
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price

    @property
    def side_es(self) -> str:
        """'compra', 'venta' or 'otro' — only for conviction-signal codes."""
        if not self.spec.is_conviction_signal:
            return "otro"
        return "compra" if self.acquired else "venta"


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        el.tag = _NS.sub("", el.tag)


def _text(node: ET.Element | None, path: str) -> str:
    """Text at `path` under `node`, unwrapping EDGAR's <value> indirection."""
    if node is None:
        return ""
    found = node.find(path)
    if found is None:
        return ""
    value = found.find("value")
    target = value if value is not None else found
    return (target.text or "").strip()


def _number(node: ET.Element | None, path: str) -> float | None:
    raw = _text(node, path)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _owner_title(owner: ET.Element | None) -> str:
    """Best available description of the insider's role, in Spanish.

    A filer can be several things at once (OXY's Richard Jackson is both
    director and CEO); the most informative label wins, and the explicit
    `officerTitle` beats the generic flags.
    """
    if owner is None:
        return "Desconocido"
    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return "Desconocido"
    title = _text(rel, "officerTitle")
    if title:
        return title
    roles = []
    if _text(rel, "isOfficer") in ("1", "true"):
        roles.append("Directivo")
    if _text(rel, "isDirector") in ("1", "true"):
        roles.append("Director")
    if _text(rel, "isTenPercentOwner") in ("1", "true"):
        roles.append("Dueño >10%")
    if _text(rel, "isOther") in ("1", "true"):
        roles.append("Otro")
    return " / ".join(roles) if roles else "Desconocido"


def _footnote_text(root: ET.Element) -> str:
    return " ".join(
        (node.text or "") for node in root.iter("footnote")
    )


def parse_form4(
    xml: str, accession: str = "", filing_date: str = ""
) -> list[Form4Transaction]:
    """Parse one Form 4 XML document into its transactions.

    Returns an empty list for a filing that reports only holdings and no
    transactions, and for XML that cannot be parsed at all — malformed
    input is not an exception here because one bad filing out of hundreds
    must not abort a ticker's whole insider history.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    _strip_ns(root)

    issuer = root.find("issuer")
    issuer_name = _text(issuer, "issuerName")
    issuer_symbol = _text(issuer, "issuerTradingSymbol")

    owner = root.find("reportingOwner")
    owner_name = ""
    if owner is not None:
        owner_id = owner.find("reportingOwnerId")
        owner_name = _text(owner_id, "rptOwnerName")
    owner_title = _owner_title(owner)

    footnotes = _footnote_text(root)
    plan_flag = (root.findtext("aff10b5One") or "").strip() in ("1", "true")
    is_10b5_1 = plan_flag or bool(_10B5_1_TEXT.search(footnotes))

    out: list[Form4Transaction] = []
    for table, is_derivative in (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    ):
        for node in root.findall(table):
            coding = node.find("transactionCoding")
            code = _text(coding, "transactionCode")
            amounts = node.find("transactionAmounts")
            acquired_disposed = _text(amounts, "transactionAcquiredDisposedCode")
            out.append(
                Form4Transaction(
                    issuer_name=issuer_name,
                    issuer_symbol=issuer_symbol,
                    owner_name=owner_name,
                    owner_title=owner_title,
                    date=_text(node, "transactionDate"),
                    code=code.upper(),
                    spec=spec_for(code),
                    shares=_number(amounts, "transactionShares"),
                    price=_number(amounts, "transactionPricePerShare"),
                    acquired=acquired_disposed.upper() == "A",
                    security=_text(node, "securityTitle"),
                    is_derivative=is_derivative,
                    is_10b5_1=is_10b5_1,
                    accession=accession,
                    filing_date=filing_date,
                )
            )
    return out
