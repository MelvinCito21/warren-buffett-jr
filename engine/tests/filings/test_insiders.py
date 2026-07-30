"""Tests for wbj.filings.insiders — the $1M threshold and aggregation.

The parser tests work from real filings. These work from constructed
transactions, because the cases that matter here are combinations
(a big award next to a small purchase) that no single filing contains.
"""

from pathlib import Path

import pytest

from wbj.filings.codes import spec_for
from wbj.filings.form4 import Form4Transaction, parse_form4
from wbj.filings.insiders import THRESHOLD_USD, summarize_insiders

FIXTURES = Path(__file__).parent.parent / "fixtures" / "edgar" / "form4"


def _tx(code, shares, price, acquired, owner="Doe Jane", title="CFO",
        date="2026-06-01", derivative=False, scheduled=False):
    return Form4Transaction(
        issuer_name="TEST CORP",
        issuer_symbol="TEST",
        owner_name=owner,
        owner_title=title,
        date=date,
        code=code,
        spec=spec_for(code),
        shares=shares,
        price=price,
        acquired=acquired,
        security="Common Stock",
        is_derivative=derivative,
        is_10b5_1=scheduled,
    )


# --- the $1M threshold -------------------------------------------------------


def test_purchase_above_threshold_is_reported():
    s = summarize_insiders([_tx("P", 50_000, 40.0, acquired=True)])

    assert len(s.purchases) == 1
    assert s.purchase_usd == pytest.approx(2_000_000.0)


def test_purchase_below_threshold_is_dropped_as_noise():
    """OXY's CEO really did buy 4,770 shares at $52.38 — $249,853.

    A genuine signal, but under the threshold `business-analysis.md` sets,
    so it does not enter the report.
    """
    transactions = parse_form4((FIXTURES / "purchase_P.xml").read_text())

    s = summarize_insiders(transactions)

    assert s.purchases == []
    assert s.has_signal is False
    assert s.excluded_count == len(transactions)


def test_threshold_is_exclusive_at_exactly_one_million():
    s = summarize_insiders([_tx("P", 1_000_000, 1.0, acquired=True)])

    assert s.purchases == []


# --- meaning is filtered before size, not after ------------------------------


def test_large_tax_withholding_never_reaches_the_report():
    """NVDA's $7.66M code-F withholding clears the threshold and is still
    excluded, because it is filtered by meaning first."""
    transactions = parse_form4((FIXTURES / "tax_F.xml").read_text())

    s = summarize_insiders(transactions)

    assert max(t.value_usd for t in transactions) > 7_000_000
    assert s.sales == []
    assert s.sale_usd == 0.0


def test_large_award_is_not_counted_as_a_purchase():
    s = summarize_insiders([_tx("A", 100_000, 200.0, acquired=True)])

    assert s.purchases == []
    assert s.purchase_usd == 0.0


def test_gift_is_counted_as_neither_side():
    transactions = parse_form4((FIXTURES / "gift_G.xml").read_text())

    s = summarize_insiders(transactions)

    assert s.purchases == [] and s.sales == []


# --- purchases and sales are never netted ------------------------------------


def test_purchase_and_sale_totals_stay_separate():
    """Netting a $5M purchase against a $5M sale into "zero sentiment"
    asserts the two are commensurable. They are not — an insider buys for
    one reason and sells for a dozen."""
    s = summarize_insiders([
        _tx("P", 100_000, 50.0, acquired=True, owner="Buyer A"),
        _tx("S", 100_000, 50.0, acquired=False, owner="Seller B"),
    ])

    assert s.purchase_usd == pytest.approx(5_000_000.0)
    assert s.sale_usd == pytest.approx(5_000_000.0)
    assert not hasattr(s, "net_usd")


# --- double counting ---------------------------------------------------------


def test_derivative_leg_is_not_counted_twice():
    """An exercise books the same shares as stock acquired and derivative
    disposed. Only the non-derivative leg may count toward volume."""
    s = summarize_insiders([
        _tx("S", 100_000, 50.0, acquired=False),
        _tx("S", 100_000, 50.0, acquired=False, derivative=True),
    ])

    assert len(s.sales) == 1
    assert s.sale_usd == pytest.approx(5_000_000.0)


def test_unknown_price_does_not_slip_past_the_threshold():
    s = summarize_insiders([_tx("S", 500_000, None, acquired=False)])

    assert s.sales == []


# --- aggregation by person ---------------------------------------------------


def test_transactions_aggregate_per_person():
    s = summarize_insiders([
        _tx("P", 50_000, 40.0, acquired=True, owner="Huang Jensen",
            title="CEO", date="2026-05-01"),
        _tx("P", 60_000, 40.0, acquired=True, owner="Huang Jensen",
            title="CEO", date="2026-06-01"),
        _tx("S", 40_000, 40.0, acquired=False, owner="Kress Colette",
            title="CFO", date="2026-04-01"),
    ])

    assert len(s.by_person) == 2
    ceo = next(p for p in s.by_person if p["name"] == "Huang Jensen")
    assert ceo["purchase_count"] == 2
    assert ceo["purchase_usd"] == pytest.approx(4_400_000.0)
    assert ceo["last_date"] == "2026-06-01"
    assert ceo["title"] == "CEO"


def test_people_are_ranked_by_total_dollars():
    s = summarize_insiders([
        _tx("P", 50_000, 40.0, acquired=True, owner="Small Fish"),
        _tx("S", 500_000, 40.0, acquired=False, owner="Big Fish"),
    ])

    assert [p["name"] for p in s.by_person] == ["Big Fish", "Small Fish"]


# --- the unthresholded flow --------------------------------------------------


def test_flow_counts_trades_below_the_reporting_threshold():
    """The flow is the coarse lens: a $200k purchase is too small for the
    highlights but is still real trading."""
    s = summarize_insiders([_tx("P", 5_000, 40.0, acquired=True)])

    assert s.purchases == []
    assert s.flow["buy_usd"] == pytest.approx(200_000.0)
    assert s.flow["buy_count"] == 1


def test_flow_still_excludes_non_conviction_codes():
    """No threshold does not mean no filtering — an award is not a trade."""
    s = summarize_insiders([
        _tx("A", 100_000, 200.0, acquired=True),
        _tx("F", 10_000, 200.0, acquired=False),
        _tx("G", 500_000, 0.0, acquired=False),
    ])

    assert s.flow == {
        "buy_usd": 0.0, "sell_usd": 0.0, "net_usd": 0.0,
        "buy_count": 0, "sell_count": 0,
    }


def test_flow_excludes_option_sales_from_stock_sales():
    """IREN's Co-CEOs disposed of 500,000 stock options each under code
    `S`. Options are not shares; counting them overstates the selling."""
    s = summarize_insiders([
        _tx("S", 1_000_000, 33.131, acquired=False),
        _tx("S", 500_000, 25.49, acquired=False, derivative=True),
    ])

    assert s.flow["sell_count"] == 1
    assert s.flow["sell_usd"] == pytest.approx(33_131_000.0)


def test_flow_keeps_both_sides_alongside_the_net():
    """A net of zero from $4M bought and $4M sold is not the same fact as
    no insider activity, so the sides survive next to the net."""
    s = summarize_insiders([
        _tx("P", 100_000, 40.0, acquired=True),
        _tx("S", 100_000, 40.0, acquired=False),
    ])

    assert s.flow["net_usd"] == 0.0
    assert s.flow["buy_usd"] == pytest.approx(4_000_000.0)
    assert s.flow["sell_usd"] == pytest.approx(4_000_000.0)


# --- disclosure --------------------------------------------------------------


def test_excluded_count_lets_the_report_state_the_denominator():
    """"3 operaciones >$1M" alone implies the company had 3 insider
    transactions. It had 40."""
    s = summarize_insiders(
        [_tx("P", 50_000, 40.0, acquired=True)]
        + [_tx("F", 100, 40.0, acquired=False) for _ in range(39)]
    )

    assert len(s.purchases) == 1
    assert s.excluded_count == 39


def test_scheduled_sales_are_counted_so_the_report_can_flag_them():
    """A 10b5-1 sale was scheduled months ahead, when the insider had no
    material information. It is still reported, but not as a signal."""
    s = summarize_insiders([
        _tx("S", 100_000, 50.0, acquired=False, scheduled=True),
        _tx("S", 100_000, 50.0, acquired=False, scheduled=False),
    ])

    assert len(s.sales) == 2
    assert s.scheduled_sale_count == 1


def test_threshold_is_exposed_for_the_report_to_state():
    s = summarize_insiders([])

    assert s.threshold_usd == THRESHOLD_USD == 1_000_000.0
    assert s.has_signal is False
