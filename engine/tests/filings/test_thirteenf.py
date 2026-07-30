"""Tests for wbj.filings.thirteenf and wbj.filings.superinvestors.

Fixtures are two consecutive real Berkshire Hathaway 13F-HR filings plus
NVIDIA's own 13F, which exists to prove what an issuer's 13F is not.
"""

from datetime import date
from pathlib import Path

import pytest

from wbj.filings.superinvestors import (
    TRACKED_FUNDS,
    find_cusip,
    scan_superinvestors,
)
from wbj.filings.thirteenf import parse_13f

FIXTURES = Path(__file__).parent.parent / "fixtures" / "edgar" / "thirteenf"

APPLE_CUSIP = "037833100"
COCA_COLA_CUSIP = "191216100"
BERKSHIRE_CIK = 1067983


@pytest.fixture
def berkshire_q1():
    return parse_13f(
        (FIXTURES / "berkshire_2026-05-15.xml").read_text(),
        fund_name="Berkshire Hathaway",
        fund_cik=BERKSHIRE_CIK,
        filing_date="2026-05-15",
        period_of_report="2026-03-31",
    )


@pytest.fixture
def berkshire_q4():
    return parse_13f(
        (FIXTURES / "berkshire_2026-02-17.xml").read_text(),
        fund_name="Berkshire Hathaway",
        fund_cik=BERKSHIRE_CIK,
        filing_date="2026-02-17",
        period_of_report="2025-12-31",
    )


# --- parsing -----------------------------------------------------------------


def test_parses_namespaced_information_table(berkshire_q1):
    """13F tables carry a default XML namespace; Form 4 documents do not."""
    assert len(berkshire_q1.holdings) == 90


def test_holding_fields_are_read(berkshire_q1):
    (row,) = [h for h in berkshire_q1.holdings if h.cusip == "674599105"]

    assert row.issuer_name == "OCCIDENTAL PETE CORP"
    assert row.shares == 264_941_431
    assert row.share_type == "SH"
    assert row.is_shares is True


def test_malformed_xml_yields_no_holdings_instead_of_raising():
    filing = parse_13f("<informationTable><unclosed>")

    assert filing.holdings == []
    assert filing.total_value_usd == 0.0


# --- the split-position bug --------------------------------------------------


def test_position_split_across_rows_is_summed(berkshire_q1):
    """Berkshire reports Apple on 12 rows, one per managing subsidiary.

    Taking the first row gives 692,000 shares against a real position of
    227,917,808 — understating it 329x, and plausibly enough that nothing
    downstream would flag it.
    """
    holding = berkshire_q1.holding_of(APPLE_CUSIP)

    assert holding.row_count == 12
    assert holding.shares == 227_917_808
    assert berkshire_q1.rows_for(APPLE_CUSIP)[0].shares == 692_000


def test_summed_position_matches_a_known_public_figure(berkshire_q1):
    """Berkshire's Coca-Cola stake is a long-standing round 400,000,000
    shares, split here across 10 rows. If the aggregation is wrong, this
    lands on some other number."""
    holding = berkshire_q1.holding_of(COCA_COLA_CUSIP)

    assert holding.shares == 400_000_000
    assert holding.row_count == 10


def test_unheld_cusip_returns_none(berkshire_q1):
    assert berkshire_q1.holding_of("00000Z999") is None


def test_principal_amounts_are_not_added_to_share_counts():
    """PRN rows report bond principal, a different unit from shares."""
    xml = """<informationTable>
      <infoTable><nameOfIssuer>X CORP</nameOfIssuer><cusip>111111111</cusip>
        <titleOfClass>COM</titleOfClass><value>100</value>
        <shrsOrPrnAmt><sshPrnamt>500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable><nameOfIssuer>X CORP</nameOfIssuer><cusip>111111111</cusip>
        <titleOfClass>NOTE</titleOfClass><value>900</value>
        <shrsOrPrnAmt><sshPrnamt>9000</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""

    holding = parse_13f(xml).holding_of("111111111")

    assert holding.shares == 500
    assert holding.row_count == 1


# --- an issuer's own 13F answers a different question ------------------------


def test_issuers_own_13f_does_not_contain_the_issuer():
    """NVIDIA files 13F-HRs for its own portfolio — Intel, CoreWeave,
    Synopsys. Reading them to answer "who owns NVDA" returns the wrong
    question's answer: NVDA is not in its own table."""
    filing = parse_13f((FIXTURES / "nvidia_own_portfolio.xml").read_text())

    names = {h.issuer_name for h in filing.holdings}
    assert "INTEL CORP" in names
    assert not any("NVIDIA" in n for n in names)


# --- staleness must travel with the data -------------------------------------


def test_staleness_note_states_the_age_in_days(berkshire_q1):
    note = berkshire_q1.staleness_note_es(today=date(2026, 7, 20))

    assert "2026-03-31" in note
    assert "111 días" in note
    assert "no es la posición de hoy" in note


def test_missing_period_is_declared_not_assumed():
    filing = parse_13f("<informationTable></informationTable>")

    assert "antigüedad desconocida" in filing.staleness_note_es()


# --- quarter-over-quarter movement -------------------------------------------


def test_reduced_position_is_detected(berkshire_q1, berkshire_q4):
    """Berkshire trimmed Bank of America between the two filings."""
    scan = scan_superinvestors(
        "060505104", {BERKSHIRE_CIK: berkshire_q1}, {BERKSHIRE_CIK: berkshire_q4}
    )

    (pos,) = scan.positions
    assert pos.change_es == "redujo"
    assert pos.change_pct < 0


def test_missing_prior_quarter_is_not_reported_as_a_new_position(berkshire_q1):
    """Unknown and "did not hold" are different facts. Collapsing them
    invents a fresh-conviction signal out of an unread filing."""
    scan = scan_superinvestors(APPLE_CUSIP, {BERKSHIRE_CIK: berkshire_q1})

    (pos,) = scan.positions
    assert pos.previous_shares is None
    assert pos.change_es == "sin comparativo"
    assert pos.change_pct is None


def test_absent_from_prior_quarter_is_a_new_position(berkshire_q1, berkshire_q4):
    """A fund that filed last quarter without the name genuinely opened it."""
    scan = scan_superinvestors(
        "00000Z999", {BERKSHIRE_CIK: berkshire_q1}, {BERKSHIRE_CIK: berkshire_q4}
    )

    assert scan.positions == []


# --- values reported in thousands --------------------------------------------


def test_filing_in_thousands_is_scaled_to_dollars():
    """Baupost still reports 13F values in thousands; the SEC moved to
    whole dollars in 2023 and not every filer followed.

    Its Alphabet row reads 1,181,131 shares worth 338,819. Taken as
    dollars that is $0.29 a share — a number that would rank Baupost's
    position below a rounding error next to funds filing in dollars.
    """
    filing = parse_13f((FIXTURES / "baupost_thousands.xml").read_text())

    assert filing.values_were_in_thousands is True
    holding = filing.holding_of("02079K107")
    implied_price = holding.value_usd / holding.shares
    assert 50 < implied_price < 1000


def test_filing_in_dollars_is_left_alone(berkshire_q1):
    assert berkshire_q1.values_were_in_thousands is False
    holding = berkshire_q1.holding_of(APPLE_CUSIP)
    implied_price = holding.value_usd / holding.shares
    assert 50 < implied_price < 1000


def test_unit_is_decided_per_filing_not_per_row():
    """One genuine sub-dollar stock must not rescale the whole book."""
    xml = """<informationTable>
      <infoTable><nameOfIssuer>PENNY CO</nameOfIssuer><cusip>111111111</cusip>
        <titleOfClass>COM</titleOfClass><value>100</value>
        <shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable><nameOfIssuer>NORMAL CO</nameOfIssuer><cusip>222222222</cusip>
        <titleOfClass>COM</titleOfClass><value>500000</value>
        <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable><nameOfIssuer>OTHER CO</nameOfIssuer><cusip>333333333</cusip>
        <titleOfClass>COM</titleOfClass><value>300000</value>
        <shrsOrPrnAmt><sshPrnamt>3000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""

    filing = parse_13f(xml)

    assert filing.values_were_in_thousands is False
    assert filing.holding_of("111111111").value_usd == 100


# --- CUSIP discovery and matching --------------------------------------------


def test_cusip_is_discovered_from_the_tables_themselves(berkshire_q1):
    """EDGAR's ticker map carries no CUSIP, so it is recovered from a
    table that names the issuer — then used for every comparison."""
    assert find_cusip([berkshire_q1], "APPLE") == APPLE_CUSIP


def test_edgar_entity_name_matches_the_13f_spelling(berkshire_q1):
    """EDGAR's companyfacts says "Apple Inc."; the filings say "APPLE INC".

    The packet passes the former, so a raw substring match finds nothing
    and the report silently claims no fund holds Apple.
    """
    assert find_cusip([berkshire_q1], "Apple Inc.") == APPLE_CUSIP
    assert find_cusip([berkshire_q1], "COCA-COLA CO") == COCA_COLA_CUSIP


def test_exact_match_beats_a_longer_name_sharing_the_prefix():
    """"APPLE" must not resolve to "APPLE HOSPITALITY REIT"."""
    xml = """<informationTable>
      <infoTable><nameOfIssuer>APPLE HOSPITALITY REIT INC</nameOfIssuer>
        <cusip>037933108</cusip><titleOfClass>COM</titleOfClass><value>500000</value>
        <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
      <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
        <cusip>037833100</cusip><titleOfClass>COM</titleOfClass><value>500000</value>
        <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
      </infoTable>
    </informationTable>"""

    assert find_cusip([parse_13f(xml)], "Apple Inc.") == APPLE_CUSIP


def test_cusip_discovery_returns_none_when_unheld(berkshire_q1):
    assert find_cusip([berkshire_q1], "NVIDIA") is None


def test_empty_hint_does_not_match_everything(berkshire_q1):
    """An empty hint is a substring of every name; returning the first
    holding's CUSIP would silently analyse the wrong company."""
    assert find_cusip([berkshire_q1], "") is None


# --- what the scan does and does not license the report to say ---------------


def test_no_holders_is_reported_as_scope_not_as_absence(berkshire_q1):
    scan = scan_superinvestors("00000Z999", {BERKSHIRE_CIK: berkshire_q1})

    assert scan.has_holders is False
    note = scan.coverage_note_es()
    assert "NO significa" in note
    assert "ninguno de esta lista" in note


def test_coverage_note_counts_funds_that_could_not_be_read(berkshire_q1):
    scan = scan_superinvestors(APPLE_CUSIP, {BERKSHIRE_CIK: berkshire_q1})

    assert scan.funds_scanned == 1
    assert scan.funds_unavailable == len(TRACKED_FUNDS) - 1
    assert "sin data disponible" in scan.coverage_note_es()


def test_positions_are_ranked_by_size(berkshire_q1, berkshire_q4):
    """Two funds, one small position and one large; the large leads."""
    scan = scan_superinvestors(
        APPLE_CUSIP,
        {BERKSHIRE_CIK: berkshire_q1, 1336528: berkshire_q4},
    )

    values = [p.holding.value_usd for p in scan.positions]
    assert values == sorted(values, reverse=True)
