"""Aggregate Form 4 transactions into what the report is allowed to say.

Two rules from `business-analysis.md` ("Quién la posee") drive everything
here:

1. **The $1M threshold.** Below it, an insider transaction is noise and
   does not enter the report. A director buying $40k of stock says
   nothing that survives being written down.
2. **A purchase is not the mirror of a sale.** An insider buys for one
   reason; they sell for a dozen. The two are reported separately and
   never netted into a single "insider sentiment" number, because netting
   silently asserts they are commensurable.

Only codes flagged `is_conviction_signal` in `wbj.filings.codes` reach
this stage — awards, gifts, tax withholding and option exercises are
excluded before the threshold is applied, not after. That ordering
matters: NVDA's Ajay Puri had $7.66M withheld for taxes under code `F`.
It clears $1M comfortably and means nothing. Filtering by size first and
by meaning second would have published it as a $7.66M insider disposal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wbj.filings.form4 import Form4Transaction

THRESHOLD_USD = 1_000_000.0


@dataclass(frozen=True)
class InsiderSummary:
    """Insider activity for one issuer, already filtered and aggregated."""

    purchases: list[Form4Transaction]
    sales: list[Form4Transaction]
    purchase_usd: float
    sale_usd: float
    by_person: list[dict]
    excluded_count: int
    scheduled_sale_count: int
    # Open-market totals with NO size threshold — the coarse lens behind the
    # buy-vs-sell bar. Still restricted to codes P and S, so it answers "how
    # much did insiders actually trade" and not "how many shares moved".
    flow: dict = field(default_factory=dict)
    threshold_usd: float = THRESHOLD_USD

    @property
    def has_signal(self) -> bool:
        return bool(self.purchases or self.sales)


def _is_open_market(t: Form4Transaction) -> bool:
    """True if `t` is a real open-market trade in the stock itself.

    Derivative rows are excluded even when the code qualifies. An option
    exercise books the same shares twice (the derivative disposed, the
    stock acquired), and a sale of options is not a sale of shares: IREN's
    two Co-CEOs each disposed of 500,000 "Stock Options (Right to Buy)"
    under code `S`, $49M nominal between them. Counting those as stock
    sales overstates what left their hands.
    """
    return (
        t.spec.is_conviction_signal
        and not t.is_derivative
        and t.value_usd is not None
    )


def _is_countable(t: Form4Transaction) -> bool:
    """True if `t` is an open-market trade above the reporting threshold."""
    return _is_open_market(t) and t.value_usd > THRESHOLD_USD


def _flow(transactions: list[Form4Transaction]) -> dict:
    """Total open-market dollars bought and sold, with no size threshold.

    Deliberately unfiltered by size: this is the fuller-picture lens that
    sits next to the >$1M highlights. `net_usd` is offered because the UI
    renders a buy-vs-sell bar from it, but the two sides are kept separate
    alongside it — a net of zero from $5M bought and $5M sold is not the
    same fact as no insider activity.
    """
    buy = sell = 0.0
    buy_n = sell_n = 0
    for t in transactions:
        if not _is_open_market(t):
            continue
        if t.acquired:
            buy += t.value_usd
            buy_n += 1
        else:
            sell += t.value_usd
            sell_n += 1
    return {
        "buy_usd": round(buy, 2),
        "sell_usd": round(sell, 2),
        "net_usd": round(buy - sell, 2),
        "buy_count": buy_n,
        "sell_count": sell_n,
    }


def summarize_insiders(
    transactions: list[Form4Transaction],
) -> InsiderSummary:
    """Filter to >$1M open-market activity and aggregate it by person.

    `excluded_count` reports how many transactions were dropped, so the
    report can say "42 operaciones, 3 superan $1M" rather than implying
    the company had three insider transactions in total.
    """
    countable = [t for t in transactions if _is_countable(t)]
    excluded = len(transactions) - len(countable)

    purchases = sorted(
        (t for t in countable if t.acquired), key=lambda t: t.date, reverse=True
    )
    sales = sorted(
        (t for t in countable if not t.acquired), key=lambda t: t.date, reverse=True
    )

    people: dict[str, dict] = {}
    for t in countable:
        person = people.setdefault(
            t.owner_name,
            {
                "name": t.owner_name,
                "title": t.owner_title,
                "purchase_usd": 0.0,
                "sale_usd": 0.0,
                "purchase_count": 0,
                "sale_count": 0,
                "last_date": "",
            },
        )
        bucket = "purchase" if t.acquired else "sale"
        person[f"{bucket}_usd"] += t.value_usd or 0.0
        person[f"{bucket}_count"] += 1
        person["last_date"] = max(person["last_date"], t.date)

    return InsiderSummary(
        purchases=purchases,
        sales=sales,
        purchase_usd=sum(t.value_usd or 0.0 for t in purchases),
        sale_usd=sum(t.value_usd or 0.0 for t in sales),
        by_person=sorted(
            people.values(),
            key=lambda p: p["purchase_usd"] + p["sale_usd"],
            reverse=True,
        ),
        excluded_count=excluded,
        scheduled_sale_count=sum(1 for t in sales if t.is_10b5_1),
        flow=_flow(transactions),
    )
