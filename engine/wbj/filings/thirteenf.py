"""Parser for SEC 13F-HR information tables (institutional holdings).

A 13F is filed by the *fund*, under the *fund's* CIK — the opposite of a
Form 4, which insiders file under the issuer's CIK. Looking up an issuer's
own CIK finds only what that issuer invests in: NVIDIA's ten 13F-HR
filings report its stakes in Intel, CoreWeave and Synopsys. They say
nothing about who owns NVIDIA. Answering "which superinvestors hold this
ticker" means reading each fund's table and looking for the ticker in it,
which is what `wbj.filings.superinvestors` does with this module.

Three limits travel with every 13F and must reach the report (they are
carried on `ThirteenFFiling`, not left to the caller to remember):

- **Up to 45 days stale.** A 13F is due 45 days after quarter end, so a
  freshly published filing already describes a portfolio up to four and a
  half months old. Never present it as a position held today.
- **Long US equities only.** Shorts, bonds, cash and foreign listings do
  not appear. A fund that is net short shows up as merely absent.
- **It is a fact, not a recommendation.** "Berkshire held X as of
  2026-03-31" is reportable. "Buffett likes X" is not.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta

# Unlike Form 4, 13F information tables always carry a default namespace
# (`http://www.sec.gov/edgar/document/thirteenf/informationtable`).
_NS = re.compile(r"^\{[^}]*\}")

# SEC deadline: 45 days after the end of the reported quarter.
REPORTING_LAG_DAYS = 45


@dataclass(frozen=True)
class Holding:
    """One position from a 13F information table.

    `row_count` is 1 for a row as filed, and higher for the aggregate
    returned by `ThirteenFFiling.holding_of`.
    """

    issuer_name: str
    cusip: str
    title_of_class: str
    value_usd: float
    shares: float
    share_type: str
    row_count: int = 1

    @property
    def is_shares(self) -> bool:
        """False for principal amounts (bonds), which are not share counts."""
        return self.share_type.upper() == "SH"


@dataclass(frozen=True)
class ThirteenFFiling:
    """One fund's 13F-HR: its holdings plus the dating the report must state."""

    fund_name: str
    fund_cik: int
    filing_date: str
    period_of_report: str
    holdings: list[Holding]
    # True if the filer reported values in thousands and they were scaled
    # to dollars on parse. Recorded, not hidden: it says the numbers were
    # adjusted rather than taken as filed.
    values_were_in_thousands: bool = False

    @property
    def total_value_usd(self) -> float:
        return sum(h.value_usd for h in self.holdings)

    def rows_for(self, cusip: str) -> list[Holding]:
        """Every row reporting `cusip`, exactly as filed."""
        target = cusip.strip().upper()
        return [h for h in self.holdings if h.cusip.strip().upper() == target]

    def holding_of(self, cusip: str) -> Holding | None:
        """The fund's TOTAL position in `cusip`, or None if it holds none.

        A 13F splits one position across a row per managing entity, so the
        total is a sum, not a lookup. Berkshire's Apple stake arrives as 12
        separate rows (GEICO, National Indemnity, and so on); the first row
        is 692,000 shares against a real position of 227,917,808. Returning
        that first row understates the position by a factor of 329, and
        does it silently — the number looks perfectly plausible.

        Only share rows (`SH`) are summed. Principal amounts (`PRN`, bonds)
        are a different unit and are never added to a share count.
        """
        rows = self.rows_for(cusip)
        if not rows:
            return None
        share_rows = [h for h in rows if h.is_shares] or rows
        first = share_rows[0]
        return Holding(
            issuer_name=first.issuer_name,
            cusip=first.cusip,
            title_of_class=first.title_of_class,
            value_usd=sum(h.value_usd for h in share_rows),
            shares=sum(h.shares for h in share_rows),
            share_type=first.share_type,
            row_count=len(share_rows),
        )

    def staleness_note_es(self, today: date | None = None) -> str:
        """Spanish sentence stating how old this snapshot is.

        The report must never show a 13F position without it.
        """
        if not self.period_of_report:
            return (
                "13F sin fecha de periodo declarada — antigüedad desconocida. "
                "No presentar como posición actual."
            )
        try:
            period = date.fromisoformat(self.period_of_report)
        except ValueError:
            return (
                f"13F con periodo ilegible ({self.period_of_report!r}). "
                "No presentar como posición actual."
            )
        reference = today or date.today()
        days = (reference - period).days
        return (
            f"Posición al {self.period_of_report} — {days} días de antigüedad. "
            f"Los 13F se publican hasta {REPORTING_LAG_DAYS} días después del "
            "cierre del trimestre; no es la posición de hoy."
        )


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        el.tag = _NS.sub("", el.tag)


def _text(node: ET.Element, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None else ""


def _number(node: ET.Element, path: str) -> float:
    raw = _text(node, path)
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


def _values_look_like_thousands(holdings: list[Holding]) -> bool:
    """True if this filing reports `value` in thousands rather than dollars.

    The SEC moved 13F values from thousands to whole dollars in 2023, and
    not every filer migrated. Baupost still files in thousands: its
    Alphabet row reads 1,181,131 shares worth 338,819, which taken as
    dollars implies $0.29 a share. Read as thousands it is $338.8M, or
    ~$287 a share — the real price.

    The unit is a property of the whole filing, so it is decided from the
    median implied price across all share positions rather than per row.
    A single position could genuinely be a sub-dollar stock; a fund whose
    entire book prices under a dollar is reporting a different unit.
    """
    prices = [
        h.value_usd / h.shares
        for h in holdings
        if h.is_shares and h.shares > 0 and h.value_usd > 0
    ]
    if not prices:
        return False
    prices.sort()
    median = prices[len(prices) // 2]
    return median < 1.0


def parse_13f(
    xml: str,
    fund_name: str = "",
    fund_cik: int = 0,
    filing_date: str = "",
    period_of_report: str = "",
) -> ThirteenFFiling:
    """Parse a 13F information table into a `ThirteenFFiling`.

    Values are normalized to whole dollars regardless of the unit the
    filer used, so positions across funds are comparable.

    Malformed XML yields a filing with no holdings rather than raising, so
    one bad filing among a scan of many funds does not abort the scan.
    """
    holdings: list[Holding] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = None

    if root is not None:
        _strip_ns(root)
        for node in root.findall("infoTable"):
            holdings.append(
                Holding(
                    issuer_name=_text(node, "nameOfIssuer"),
                    cusip=_text(node, "cusip"),
                    title_of_class=_text(node, "titleOfClass"),
                    value_usd=_number(node, "value"),
                    shares=_number(node, "shrsOrPrnAmt/sshPrnamt"),
                    share_type=_text(node, "shrsOrPrnAmt/sshPrnamtType"),
                )
            )

    in_thousands = _values_look_like_thousands(holdings)
    if in_thousands:
        holdings = [
            Holding(
                issuer_name=h.issuer_name,
                cusip=h.cusip,
                title_of_class=h.title_of_class,
                value_usd=h.value_usd * 1000.0,
                shares=h.shares,
                share_type=h.share_type,
                row_count=h.row_count,
            )
            for h in holdings
        ]

    return ThirteenFFiling(
        fund_name=fund_name,
        fund_cik=fund_cik,
        filing_date=filing_date,
        period_of_report=period_of_report,
        holdings=holdings,
        values_were_in_thousands=in_thousands,
    )


def expected_period_for(filing_date: str) -> str:
    """Roughly the quarter-end a filing on `filing_date` should report.

    Used only to sanity-check a filing whose cover page omits the period;
    it is an inference, never presented as the filing's own statement.
    """
    try:
        filed = date.fromisoformat(filing_date)
    except (ValueError, TypeError):
        return ""
    return (filed - timedelta(days=REPORTING_LAG_DAYS)).isoformat()
