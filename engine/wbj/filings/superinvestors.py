"""Find which tracked superinvestors hold a ticker, and whether they moved.

The direction of the lookup is the whole design problem. Form 4 lets you
start from the issuer: insiders file under the issuer's CIK, so NVDA's CIK
yields NVDA's insiders. A 13F is filed by the fund under the fund's CIK,
so there is no query that asks EDGAR "who holds NVDA". The only way is to
read each fund's table and look for the ticker inside it. That is what
commercial aggregators sell; it is a scan, not a lookup.

Which funds get scanned is therefore a curated list, and curation is a
judgement the system must not hide. `TRACKED_FUNDS` below is a starting
set of managers whose filings are worth reading — it is not "the
superinvestors", it is the ones this system happens to watch. A ticker
absent from the results is absent *from this list*, which is a weaker
statement than "no notable investor holds it", and `scan_superinvestors`
reports it as such.

Matching is by CUSIP, never by name. Funds spell issuers differently
("NVIDIA CORP", "NVIDIA CORPORATION") and a name match would both miss
holdings and, worse, conflate different issuers that share a word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from wbj.filings.thirteenf import Holding, ThirteenFFiling

# Managers this system reads, by SEC CIK. Deliberately short and explicit:
# a long list quietly becomes "everyone", and the report's claim that a
# ticker is unheld would then be doing work the data cannot support.
#
# `edgar_name` is the entity name EDGAR returns for that CIK, recorded so a
# wrong number is caught instead of silently attributing one manager's book
# to another. That is not hypothetical: CIK 1637460 is Man Group plc, and
# was very nearly shipped here labelled as Michael Burry's Scion. Every CIK
# below was checked against `data.sec.gov/submissions/` on 2026-07-20;
# `scripts/verify_fund_ciks.py` re-checks them.
TRACKED_FUNDS_META: dict[int, dict[str, str]] = {
    1067983: {
        "label": "Berkshire Hathaway (Warren Buffett)",
        "edgar_name": "BERKSHIRE HATHAWAY INC",
    },
    1336528: {
        "label": "Pershing Square (Bill Ackman)",
        "edgar_name": "Pershing Square Capital Management, L.P.",
    },
    1061768: {
        "label": "Baupost Group (Seth Klarman)",
        "edgar_name": "BAUPOST GROUP LLC/MA",
    },
    1656456: {
        "label": "Appaloosa (David Tepper)",
        "edgar_name": "Appaloosa LP",
    },
    1350694: {
        "label": "Bridgewater Associates (Ray Dalio)",
        "edgar_name": "Bridgewater Associates, LP",
    },
    1649339: {
        "label": "Scion Asset Management (Michael Burry)",
        "edgar_name": "Scion Asset Management, LLC",
    },
    1536411: {
        "label": "Duquesne Family Office (Stanley Druckenmiller)",
        "edgar_name": "Duquesne Family Office LLC",
    },
}


# Index managers are deliberately absent. BlackRock and Vanguard hold
# essentially every US listed company, so their presence carries no
# information about conviction — including them would put a holder on
# every ticker and make the section always say the same thing.

TRACKED_FUNDS: dict[int, str] = {
    cik: meta["label"] for cik, meta in TRACKED_FUNDS_META.items()
}


@dataclass(frozen=True)
class Position:
    """One tracked fund's position in the ticker, with its quarter-on-quarter move."""

    fund_name: str
    fund_cik: int
    holding: Holding
    filing_date: str
    period_of_report: str
    previous_shares: float | None
    staleness_note_es: str

    @property
    def change_es(self) -> str:
        """'nueva' / 'aumentó' / 'redujo' / 'sin cambio' / 'sin comparativo'.

        `previous_shares` is None when the prior quarter's filing was not
        read, which is not the same as the fund not having held it. The
        two must not collapse into "nueva posición" — that would invent a
        conviction signal out of missing data.
        """
        if self.previous_shares is None:
            return "sin comparativo"
        if self.previous_shares == 0:
            return "nueva"
        if self.holding.shares > self.previous_shares:
            return "aumentó"
        if self.holding.shares < self.previous_shares:
            return "redujo"
        return "sin cambio"

    @property
    def change_pct(self) -> float | None:
        if not self.previous_shares:
            return None
        return (self.holding.shares - self.previous_shares) / self.previous_shares * 100


@dataclass(frozen=True)
class SuperinvestorScan:
    """Result of scanning the tracked funds for one ticker."""

    cusip: str
    positions: list[Position]
    funds_scanned: int
    funds_unavailable: int

    @property
    def has_holders(self) -> bool:
        return bool(self.positions)

    def coverage_note_es(self) -> str:
        """What this scan does and does not license the report to say."""
        base = (
            f"Revisados {self.funds_scanned} de {len(TRACKED_FUNDS)} fondos "
            "de la lista que sigue el sistema"
        )
        if self.funds_unavailable:
            base += f" ({self.funds_unavailable} sin data disponible)"
        if not self.has_holders:
            return (
                base + ". Ninguno reporta esta posición. Eso NO significa que "
                "ningún inversionista importante la tenga — significa que "
                "ninguno de esta lista la reporta en su último 13F."
            )
        return base + "."


# Corporate suffixes and punctuation differ between EDGAR's entity name
# and how funds spell the issuer on a 13F: EDGAR says "Apple Inc.", the
# filings say "APPLE INC". Comparing raw strings misses that.
_NOISE = re.compile(r"[^A-Z0-9 ]+")
_SUFFIXES = (
    " INCORPORATED", " INC", " CORPORATION", " CORP", " COMPANY", " CO",
    " LIMITED", " LTD", " PLC", " LLC", " LP", " NV", " SA", " AG",
    " HOLDINGS", " HOLDING", " GROUP", " CLASS A", " CL A", " COM",
)


def _normalize_issuer(name: str) -> str:
    """Reduce an issuer name to a comparable core: 'Apple Inc.' -> 'APPLE'."""
    text = _NOISE.sub(" ", (name or "").upper())
    text = " ".join(text.split())
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def find_cusip(filings: list[ThirteenFFiling], issuer_hint: str) -> str | None:
    """Learn a ticker's CUSIP from tables that already name the issuer.

    EDGAR's ticker map carries no CUSIP, and CUSIP is licensed data, so it
    is recovered from the 13F tables themselves: whichever fund holds the
    issuer states both its name and its CUSIP. The name match is used only
    to *discover* the identifier — every subsequent comparison is by CUSIP.

    An exact match on the normalized core is tried across every filing
    before any prefix match, so "APPLE" cannot lose to a fund that happens
    to list "APPLE HOSPITALITY REIT" earlier in its table.
    """
    hint = _normalize_issuer(issuer_hint)
    if not hint:
        return None

    candidates = [
        (_normalize_issuer(h.issuer_name), h.cusip)
        for filing in filings
        for h in filing.holdings
    ]
    for name, cusip in candidates:
        if name == hint:
            return cusip
    for name, cusip in candidates:
        if name.startswith(hint + " ") or hint.startswith(name + " "):
            return cusip
    return None


def scan_superinvestors(
    cusip: str,
    current: dict[int, ThirteenFFiling],
    previous: dict[int, ThirteenFFiling] | None = None,
) -> SuperinvestorScan:
    """Collect tracked funds' positions in `cusip`, newest quarter first.

    `current` and `previous` map fund CIK to that fund's latest and prior
    13F. A fund missing from `previous` yields `previous_shares=None`
    (unknown), never 0 (did not hold) — see `Position.change_es`.
    """
    previous = previous or {}
    positions: list[Position] = []

    for cik, filing in current.items():
        holding = filing.holding_of(cusip)
        if holding is None:
            continue
        prior_filing = previous.get(cik)
        if prior_filing is None:
            previous_shares = None
        else:
            prior_holding = prior_filing.holding_of(cusip)
            previous_shares = prior_holding.shares if prior_holding else 0.0

        positions.append(
            Position(
                fund_name=TRACKED_FUNDS.get(cik, filing.fund_name or f"CIK {cik}"),
                fund_cik=cik,
                holding=holding,
                filing_date=filing.filing_date,
                period_of_report=filing.period_of_report,
                previous_shares=previous_shares,
                staleness_note_es=filing.staleness_note_es(),
            )
        )

    positions.sort(key=lambda p: p.holding.value_usd, reverse=True)
    return SuperinvestorScan(
        cusip=cusip,
        positions=positions,
        funds_scanned=len(current),
        funds_unavailable=len(TRACKED_FUNDS) - len(current),
    )
