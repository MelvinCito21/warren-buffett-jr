"""Tests for the MVP packet helpers in wbj.cli (the builder every command uses)."""

from wbj.cli import _annual_series, _is_full_period


def _facts(tag_rows: dict[str, list[dict]]) -> dict:
    """Wrap {tag: [row, ...]} in the SEC companyfacts envelope."""
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tag_rows.items()}}}


def _row(start, end, val, filed="2026-01-01"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-K", "fp": "FY"}


# --- full-period filter ------------------------------------------------------


def test_quarterly_rows_inside_a_10k_are_excluded():
    """A 10-K tags its quarterly durations `fp=FY` too; only ~1-year rows are
    annual, else Q1-Q3 land as extra points on the same calendar year."""
    q2 = _row("2019-01-28", "2019-04-28", 2_200)
    fy = _row("2018-01-29", "2019-01-27", 11_700)

    out = _annual_series(_facts({"Revenues": [q2, fy]}), ["Revenues"])

    assert [r["val"] for r in out] == [11_700]


def test_balance_sheet_instants_are_kept():
    """Instants (equity, debt) carry no `start` and must survive the filter."""
    instant = {"end": "2026-01-25", "val": 80_000, "filed": "2026-03-01",
               "form": "10-K", "fp": "FY"}

    out = _annual_series(_facts({"StockholdersEquity": [instant]}), ["StockholdersEquity"])

    assert [r["val"] for r in out] == [80_000]


def test_is_full_period_accepts_53_week_year_and_rejects_a_quarter():
    assert _is_full_period(_row("2025-01-27", "2026-01-25", 1)) is True
    assert _is_full_period(_row("2025-10-27", "2026-01-25", 1)) is False


# --- tag migration -----------------------------------------------------------


def test_series_spans_a_mid_history_tag_migration():
    """NVDA moved off `RevenueFromContractWithCustomer...` after FY2022; taking
    the first tag with *any* data truncated revenue there while net income ran
    to FY2026, producing an impossible >100% margin."""
    old_tag = [_row("2021-02-01", "2022-01-30", 26_900)]
    new_tag = [
        _row("2021-02-01", "2022-01-30", 26_900),
        _row("2025-01-27", "2026-01-25", 215_900),
    ]
    facts = _facts({
        "RevenueFromContractWithCustomerExcludingAssessedTax": old_tag,
        "Revenues": new_tag,
    })

    out = _annual_series(facts, [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
    ])

    assert [r["val"] for r in out] == [26_900, 215_900]


def test_earlier_tag_wins_a_year_both_tags_report():
    facts = _facts({
        "RevenueFromContractWithCustomerExcludingAssessedTax":
            [_row("2025-01-27", "2026-01-25", 111)],
        "Revenues": [_row("2025-01-27", "2026-01-25", 999)],
    })

    out = _annual_series(facts, [
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
    ])

    assert [r["val"] for r in out] == [111]


def test_latest_filing_wins_a_restated_year():
    rows = [
        _row("2025-01-27", "2026-01-25", 100, filed="2026-03-01"),
        _row("2025-01-27", "2026-01-25", 120, filed="2026-09-01"),
    ]

    out = _annual_series(_facts({"Revenues": rows}), ["Revenues"])

    assert [r["val"] for r in out] == [120]


def test_missing_tags_yield_empty_series():
    assert _annual_series(_facts({}), ["Revenues"]) == []
