"""Tests for wbj.filings.form4 — one case per transaction code that matters.

Every fixture is a real filing pulled from EDGAR, not a hand-written
sample, because the failure mode being guarded against is a real filing
whose shape was not anticipated.
"""

from pathlib import Path

import pytest

from wbj.filings.codes import Meaning, spec_for
from wbj.filings.form4 import parse_form4

FIXTURES = Path(__file__).parent.parent / "fixtures" / "edgar" / "form4"


def _parse(name):
    return parse_form4((FIXTURES / f"{name}.xml").read_text())


def _by_code(name, code):
    return [t for t in _parse(name) if t.code == code]


# --- P: open-market purchase, the strong signal ------------------------------


def test_purchase_p_is_a_conviction_signal_on_the_buy_side():
    """OXY's President and CEO bought 4,770 shares at $52.38."""
    (t,) = _by_code("purchase_P", "P")

    assert t.owner_name == "Jackson Richard A."
    assert t.owner_title == "President and CEO"
    assert t.spec.meaning is Meaning.OPEN_MARKET_PURCHASE
    assert t.spec.is_conviction_signal is True
    assert t.acquired is True
    assert t.side_es == "compra"
    assert t.value_usd == pytest.approx(4770 * 52.38)


# --- S: open-market sale, a weak signal --------------------------------------


def test_sale_s_is_a_conviction_signal_on_the_sell_side():
    """NVDA director Stephen Neal sold 15,500 shares at $215.7331."""
    sales = _by_code("sale_S", "S")

    assert sales, "the fixture must contain at least one S transaction"
    t = sales[0]
    assert t.owner_name == "Neal Stephen C"
    assert t.spec.meaning is Meaning.OPEN_MARKET_SALE
    assert t.spec.is_conviction_signal is True
    assert t.acquired is False
    assert t.side_es == "venta"


# --- G: gift — the case that breaks naive parsers ----------------------------


def test_gift_g_is_not_read_as_a_sale():
    """Tench Coxe moved 500,000 NVDA shares at $0.00 under code G.

    The acquired/disposed flag says `D`. A parser keying on that flag
    alone reports a 500,000-share disposal and implies the director is
    exiting. He gifted them to a trust; nobody sold and nobody paid.
    """
    (t,) = _by_code("gift_G", "G")

    assert t.owner_name == "COXE TENCH"
    assert t.shares == 500_000
    assert t.acquired is False, "the raw filing does say disposed"
    # ...but it is not a sale, and must never be reported as one.
    assert t.spec.meaning is Meaning.GIFT
    assert t.spec.is_conviction_signal is False
    assert t.side_es == "otro"


def test_gift_g_carries_no_dollar_value():
    """A gift reports $0.00 because no money changed hands — a real zero."""
    (t,) = _by_code("gift_G", "G")

    assert t.price == 0.0
    assert t.value_usd == 0.0


# --- A: award — compensation, not conviction ---------------------------------


def test_award_a_is_not_a_purchase():
    """The annual director grant is not the insider choosing to buy."""
    awards = _by_code("award_A", "A")

    assert awards
    for t in awards:
        assert t.acquired is True, "an award does acquire shares"
        assert t.spec.meaning is Meaning.AWARD
        assert t.spec.is_conviction_signal is False
        assert t.side_es == "otro"


# --- F: tax withholding — noise that clears the $1M bar ----------------------


def test_tax_withholding_f_is_noise_despite_being_large():
    """NVDA's Ajay Puri had 36,927 shares withheld at $207.41 — $7.66M.

    Size alone would promote this to the report's headline. It is the
    automatic tax withholding on a vesting, not a decision to sell.
    """
    (t,) = _by_code("tax_F", "F")

    assert t.value_usd > 7_000_000
    assert t.spec.meaning is Meaning.TAX_WITHHOLDING
    assert t.spec.is_conviction_signal is False
    assert t.side_es == "otro"


# --- M: option exercise — the classic mistake --------------------------------


def test_option_exercise_m_is_not_a_purchase():
    exercises = _by_code("exercise_M", "M")

    assert exercises
    for t in exercises:
        assert t.spec.meaning is Meaning.OPTION_EXERCISE
        assert t.spec.is_conviction_signal is False


def test_option_exercise_books_the_same_shares_twice():
    """Ford's exercise appears as stock acquired AND the unit disposed.

    Both legs are real and both are parsed; the `is_derivative` flag is
    what lets the aggregator avoid counting 74,098 shares as 148,196.
    """
    exercises = _by_code("exercise_M", "M")

    assert len(exercises) == 2
    stock, derivative = sorted(exercises, key=lambda t: t.is_derivative)
    assert stock.is_derivative is False and stock.acquired is True
    assert derivative.is_derivative is True and derivative.acquired is False
    assert stock.shares == derivative.shares == 74_098


def test_missing_price_is_not_treated_as_zero():
    """An option exercise omits the price; that is unknown, not free.

    Returning 0.0 would let a transaction of unknown size pass through
    the $1M filter as if it were worth nothing.
    """
    (t,) = [t for t in _by_code("exercise_M", "M") if not t.is_derivative]

    assert t.price is None
    assert t.value_usd is None


# --- J: transfer between the insider's own entities --------------------------


def test_transfer_j_appears_on_both_sides_and_nets_to_nothing():
    """Jensen Huang's code-J rows show the same share counts acquired
    and disposed — a move between his own entities, not an exit.

    Summing only the disposed side reports a ~49M-share sell-off that
    never happened.
    """
    js = _by_code("transfer_J", "J")

    acquired = sorted(t.shares for t in js if t.acquired)
    disposed = sorted(t.shares for t in js if not t.acquired)

    assert acquired, "the fixture must have both sides"
    # Every acquired amount is matched by an identical disposed amount.
    assert set(acquired).issubset(set(disposed))
    for t in js:
        assert t.spec.is_conviction_signal is False


# --- 10b5-1 scheduled plans --------------------------------------------------


def test_aff10b5one_flag_marks_a_scheduled_transaction():
    """The gift fixture carries `aff10b5One` = 1 at document level."""
    (t,) = _by_code("gift_G", "G")

    assert t.is_10b5_1 is True


def test_absent_flag_leaves_transaction_unscheduled():
    (t,) = _by_code("purchase_P", "P")

    assert t.is_10b5_1 is False


# --- structural guarantees ---------------------------------------------------


def test_holdings_are_not_reported_as_transactions():
    """The gift fixture has two `nonDerivativeHolding` rows alongside the
    one real transaction. Holdings report a standing position with no
    transaction; counting them invents activity."""
    transactions = _parse("gift_G")

    assert len(transactions) == 1


def test_issuer_and_filing_metadata_are_carried_through():
    transactions = parse_form4(
        (FIXTURES / "gift_G.xml").read_text(),
        accession="0001197647-26-000005",
        filing_date="2026-07-06",
    )

    (t,) = transactions
    assert t.issuer_name == "NVIDIA CORP"
    assert t.issuer_symbol == "NVDA"
    assert t.accession == "0001197647-26-000005"
    assert t.filing_date == "2026-07-06"


def test_malformed_xml_yields_no_transactions_instead_of_raising():
    """One unparseable filing must not abort a ticker's whole history."""
    assert parse_form4("<ownershipDocument><unclosed>") == []


def test_unknown_code_is_surfaced_not_guessed():
    """An unrecognized code must not be silently folded into 'sale'."""
    spec = spec_for("Q")

    assert spec.is_conviction_signal is False
    assert spec.label_es == "Código no reconocido"
