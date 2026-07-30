"""Quick 6-agent scorecard (1-10 per category) from the MVP EDGAR packet.

Honest-scoring rules (Cerebro: "sin evidencia, no hay número"):
- Financial, Business, Risk: scored from real EDGAR fundamentals (partial
  metric sets — a *quick* read, not the full specialist methodology).
- Market, Technical, Valuation: scored from FMP market data when present
  (analyst estimates / adjusted OHLCV / price + market cap) — a *quick*
  read too, still not the full specialist engines (Phase 2). Each stays
  NOT_SCORABLE when its FMP data is absent — never imputed.
- Overall 1-10 weights only the covered categories and reports how many
  of the 100 evidence points that covers.
"""

from __future__ import annotations

from statistics import pstdev

from wbj.core.formulas import yoy
from wbj.core.nullstates import EvidenceClass, NullState, Value
from wbj.core.scoring import CATEGORY_WEIGHTS, Category, Dimension, anchor_score

# --- Anchors aligned with Cerebro FIN/BUS band cutoffs (quick defaults) ---
_A_REV_GROWTH = [(-0.10, 0.0), (0.0, 3.0), (0.10, 6.0), (0.25, 9.0), (0.40, 10.0)]
_A_NET_MARGIN = [(-0.10, 0.0), (0.0, 3.0), (0.10, 6.0), (0.20, 8.5), (0.30, 10.0)]
_A_FCF_MARGIN = [(-0.10, 0.0), (0.0, 3.0), (0.10, 6.5), (0.25, 10.0)]
_A_DEBT_EQUITY = [(0.0, 10.0), (0.5, 8.0), (1.0, 6.0), (2.0, 3.0), (4.0, 0.0)]
_A_OP_MARGIN = [(0.0, 0.0), (0.10, 4.0), (0.20, 7.0), (0.35, 10.0)]
_A_GROSS_MARGIN = [(0.0, 0.0), (0.20, 4.0), (0.40, 7.0), (0.60, 10.0)]
_A_ROE = [(0.0, 0.0), (0.08, 4.0), (0.15, 7.0), (0.30, 10.0)]
# margin stability: 5y net-margin std-dev (lower = steadier = moat signal)
_A_MARGIN_STDEV = [(0.01, 10.0), (0.03, 7.0), (0.06, 4.0), (0.12, 0.0)]
# interest coverage: <1.5x solvency warning band, >5x comfortable
_A_INT_COVERAGE = [(0.0, 0.0), (1.5, 2.0), (3.0, 5.0), (5.0, 7.0), (10.0, 10.0)]

# --- Phase 1 quick FMP anchors (market / technical / valuation) ---
# forward revenue growth reuses the revenue-growth shape (_A_REV_GROWTH)
_A_ANALYST_BREADTH = [(0.0, 0.0), (3.0, 4.0), (10.0, 7.0), (25.0, 10.0)]
_A_PRICE_SMA = [(0.85, 2.0), (1.0, 5.0), (1.10, 8.0), (1.25, 10.0)]
_A_MOMENTUM_6M = [(-0.20, 0.0), (0.0, 4.0), (0.15, 7.0), (0.40, 10.0)]
_A_OFF_HIGH = [(-0.40, 2.0), (-0.20, 5.0), (-0.08, 8.0), (0.0, 10.0)]
# P/E and P/FCF: lower is cheaper (descending scores; anchor_score interpolates)
_A_PE = [(10.0, 10.0), (18.0, 8.0), (28.0, 5.0), (45.0, 2.0), (70.0, 0.0)]
_A_PFCF = [(12.0, 10.0), (22.0, 8.0), (35.0, 5.0), (55.0, 2.0), (90.0, 0.0)]

_TECHNICAL_MIN_SESSIONS = 200

_QUICK_LABEL = {
    "business": "Business (quick)",
    "financial": "Financial (quick)",
    "market": "Market & Growth (quick)",
    "technical": "Technical & Momentum (quick)",
    "risk": "Risk & Resilience (quick)",
    "valuation": "Valuation (quick)",
}
# Default N/S reason per category: the benign, company-side explanation,
# used only when the data is genuinely absent (a 200 with nothing in it).
_NS_REASON = {
    "market": "sin cobertura de analistas (FMP)",
    "technical": "historial de precio insuficiente (FMP)",
    "valuation": "sin precio de mercado (FMP)",
}

# Which FMP endpoint's status decides whether a category was plan-blocked
# rather than genuinely empty. Keyed to the sources recorded in the
# packet's `fmp_status` block by `_build_packet`.
_CATEGORY_SOURCE = {
    "market": "estimates",
    "technical": "ohlcv",
    "valuation": "profile",
}

_PLAN_BLOCKED_REASON = (
    "bloqueado por el plan de FMP — el dato existe, la suscripción no lo "
    "alcanza (no es que la empresa no lo tenga)"
)


def _ns_reason(key: str, fmp_status: dict | None) -> str:
    """Honest reason a category is N/S: distinguishes a plan block (402)
    from data the company genuinely lacks.

    A 402 means FMP has the data but this subscription (or the ticker's
    universe) does not reach it — reporting that as "no analyst coverage"
    would put a negative fact on the company that isn't true. Any other
    outcome falls back to the benign default.
    """
    source = _CATEGORY_SOURCE.get(key)
    status = (fmp_status or {}).get(source) if source else None
    if status == 402:
        return _PLAN_BLOCKED_REASON
    return _NS_REASON[key]


def _val(x: float | None, name: str, unit: str = "ratio") -> Value:
    if x is None:
        return Value.null(NullState.MISSING, unit=unit, warnings=[f"MISSING: {name}"])
    return Value.of(x, unit=unit, evidence_class=EvidenceClass.C)


def _scored(v: Value, anchors: list[tuple[float, float]]) -> Value:
    if v.is_null:
        return v
    return Value.of(anchor_score(v.value, anchors), unit="score", evidence_class=EvidenceClass.C)


def _latest(series: list[dict]) -> float | None:
    return series[-1]["val"] if series else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


def _margin_history(ni: list[dict], rev: list[dict], years: int = 5) -> list[float]:
    by_end = {r["end"]: r["val"] for r in rev if r.get("val")}
    pairs = [(r["val"] / by_end[r["end"]]) for r in ni if r["end"] in by_end]
    return pairs[-years:]


def _dim(name: str, max_points: float, scores: list[Value]) -> Dimension:
    w = 1.0 / len(scores)
    return Dimension(name=name, max_points=max_points, metric_scores=[(w, s) for s in scores])


# --- Phase 1 quick FMP helpers (pure over the packet's market_data block) ---


def _closes_chrono(ohlcv: list[dict] | None) -> list[float]:
    """FMP daily bars (any order) -> ascending list of adjusted closes.

    Prefers `adjClose` when the feed provides it, else `close` (the
    `/stable/historical-price-eod/full` fixture carries only `close`).
    """
    if not ohlcv:
        return []
    rows = sorted(ohlcv, key=lambda r: r.get("date", ""))
    closes: list[float] = []
    for r in rows:
        c = r.get("adjClose", r.get("close"))
        if c is not None:
            closes.append(float(c))
    return closes


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _momentum(closes: list[float], lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    past = closes[-1 - lookback]
    return (closes[-1] / past - 1.0) if past else None


def _off_high(closes: list[float], window: int = 252) -> float | None:
    """Fraction below the trailing-`window` high (0 = at the high, <0 below)."""
    if not closes:
        return None
    high = max(closes[-window:])
    return (closes[-1] / high - 1.0) if high else None


def _nearest_future_estimate(estimates: list[dict] | None, as_of: str) -> dict | None:
    """Soonest analyst-estimate row dated strictly after `as_of`."""
    future = [e for e in (estimates or []) if e.get("date", "") > as_of]
    return min(future, key=lambda e: e["date"]) if future else None


def _first_present(row: dict, *keys: str) -> float | None:
    """First non-None value among `keys` (tolerates FMP field-name drift:
    live `/stable/` uses `revenueAvg`/`numAnalystsRevenue`, legacy `/api/v3/`
    used `estimatedRevenueAvg`/`numberAnalystEstimatedRevenue`)."""
    for k in keys:
        v = row.get(k)
        if v is not None:
            return v
    return None


def _market_category(md: dict, rev: list[dict], as_of: str) -> Category:
    """Market & Growth (20 pts): forward revenue growth + analyst breadth."""
    row = _nearest_future_estimate(md.get("estimates"), as_of)
    latest_rev = _latest(rev)
    fwd = _first_present(row, "revenueAvg", "estimatedRevenueAvg") if row else None
    growth = (fwd / latest_rev - 1.0) if (fwd and latest_rev) else None
    breadth = _first_present(row, "numAnalystsRevenue", "numberAnalystEstimatedRevenue") if row else None
    return Category(name="market", max_points=20.0, dimensions=[
        _dim("Growth outlook", 20.0, [
            _scored(_val(growth, "forward_rev_growth"), _A_REV_GROWTH),
            _scored(_val(breadth, "analyst_breadth", unit="count"), _A_ANALYST_BREADTH),
        ]),
    ])


def _technical_category(md: dict) -> Category | None:
    """Technical & Momentum (20 pts): price vs SMA50/SMA200, 6-mo momentum,
    % off the 52-week high. Needs >=200 sessions of history (else N/S)."""
    closes = _closes_chrono(md.get("ohlcv"))
    if len(closes) < _TECHNICAL_MIN_SESSIONS:
        return None
    price = closes[-1]
    return Category(name="technical", max_points=20.0, dimensions=[
        _dim("Trend", 10.0, [
            _scored(_val(_ratio(price, _sma(closes, 50)), "price_vs_sma50"), _A_PRICE_SMA),
            _scored(_val(_ratio(price, _sma(closes, 200)), "price_vs_sma200"), _A_PRICE_SMA),
        ]),
        _dim("Momentum", 10.0, [
            _scored(_val(_momentum(closes, 126), "momentum_6m"), _A_MOMENTUM_6M),
            _scored(_val(_off_high(closes), "off_52w_high"), _A_OFF_HIGH),
        ]),
    ])


def _valuation_category(md: dict, annual: dict) -> Category | None:
    """Valuation (10 pts): P/E and P/FCF. A non-meaningful multiple (EPS or
    FCF <= 0) is dropped; both non-meaningful or no price -> N/S."""
    price = md.get("price")
    if not price:
        return None
    ni_l = _latest(annual["net_income"])
    shares_l = _latest(annual.get("diluted_shares", []))
    ocf_l, capex_l = _latest(annual["operating_cash_flow"]), _latest(annual["capex"])
    eps = (ni_l / shares_l) if (ni_l is not None and shares_l) else None
    fcf = (ocf_l - capex_l) if (ocf_l is not None and capex_l is not None) else None
    pe = (price / eps) if (eps and eps > 0) else None
    pfcf = (md.get("market_cap") / fcf) if (md.get("market_cap") and fcf and fcf > 0) else None

    scores: list[Value] = []
    if pe is not None:
        scores.append(_scored(_val(pe, "pe", unit="x"), _A_PE))
    if pfcf is not None:
        scores.append(_scored(_val(pfcf, "pfcf", unit="x"), _A_PFCF))
    if not scores:
        return None
    return Category(name="valuation", max_points=10.0,
                    dimensions=[_dim("Multiples", 10.0, scores)])


def quick_scorecard(packet: dict) -> dict:
    """Compute the quick 6-category scorecard from an MVP packet."""
    a = packet["annual"]
    rev, ni = a["revenue"], a["net_income"]
    ocf, capex = a["operating_cash_flow"], a["capex"]
    debt, eq = a["long_term_debt"], a["equity"]
    op = a.get("operating_income", [])
    gp = a.get("gross_profit", [])
    interest = a.get("interest_expense", [])

    rev_l, ni_l = _latest(rev), _latest(ni)
    ocf_l, capex_l = _latest(ocf), _latest(capex)
    debt_l, eq_l = _latest(debt), _latest(eq)
    op_l, gp_l, int_l = _latest(op), _latest(gp), _latest(interest)

    growth = (
        yoy(rev[-1]["val"], rev[-2]["val"])
        if len(rev) >= 2
        else Value.null(NullState.MISSING, unit="ratio", warnings=["MISSING: revenue history"])
    )
    fcf = (ocf_l - capex_l) if ocf_l is not None and capex_l is not None else None
    margins = _margin_history(ni, rev)
    margin_stdev = _val(pstdev(margins) if len(margins) >= 3 else None, "margin_stability")
    # ROE on average equity when two years exist (Cerebro convention).
    eq_avg = (eq[-1]["val"] + eq[-2]["val"]) / 2 if len(eq) >= 2 else eq_l
    roe = _val(_ratio(ni_l, eq_avg), "roe")

    net_margin = _val(_ratio(ni_l, rev_l), "net_margin")
    fcf_margin = _val(_ratio(fcf, rev_l), "fcf_margin")
    d_e = _val(_ratio(debt_l, eq_l), "debt_to_equity")
    op_margin = _val(_ratio(op_l, rev_l), "operating_margin")
    gross_margin = _val(_ratio(gp_l, rev_l), "gross_margin")
    # EBIT proxy / interest expense; interest of 0/missing -> NOT_MEANINGFUL.
    coverage = (
        _val(_ratio(op_l, int_l), "interest_coverage")
        if int_l
        else Value.null(NullState.NOT_MEANINGFUL, unit="x", warnings=["no interest expense reported"])
    )
    fcf_positive = _val(None if fcf is None else (1.0 if fcf > 0 else 0.0), "fcf_positive")

    categories: dict[str, Category] = {
        "financial": Category(name="financial", max_points=15.0, dimensions=[
            _dim("Profitability", 7.5, [_scored(net_margin, _A_NET_MARGIN),
                                        _scored(fcf_margin, _A_FCF_MARGIN)]),
            _dim("Growth & Balance Sheet", 7.5, [_scored(growth, _A_REV_GROWTH),
                                                 _scored(d_e, _A_DEBT_EQUITY)]),
        ]),
        "business": Category(name="business", max_points=20.0, dimensions=[
            _dim("Margin power", 10.0, [_scored(op_margin, _A_OP_MARGIN),
                                        _scored(gross_margin, _A_GROSS_MARGIN)]),
            _dim("Durability & returns", 10.0, [_scored(margin_stdev, _A_MARGIN_STDEV),
                                                _scored(roe, _A_ROE)]),
        ]),
        "risk": Category(name="risk", max_points=15.0, dimensions=[
            _dim("Solvency", 7.5, [_scored(d_e, _A_DEBT_EQUITY),
                                   _scored(coverage, _A_INT_COVERAGE)]),
            _dim("Cash generation", 7.5, [_scored(fcf_positive, [(0.0, 2.0), (1.0, 10.0)]),
                                          _scored(fcf_margin, _A_FCF_MARGIN)]),
        ]),
    }

    # Quick FMP categories (Phase 1): only included when their data covers at
    # least one metric — otherwise they fall through to the N/S branch with a
    # truthful reason ("sin evidencia, no hay número").
    md = packet.get("market_data") or {}
    as_of = packet.get("as_of", "")
    fmp_builders = {
        "market": _market_category(md, rev, as_of),
        "technical": _technical_category(md),
        "valuation": _valuation_category(md, a),
    }
    for key, cat in fmp_builders.items():
        if cat is not None and cat.coverage() > 0:
            categories[key] = cat

    rows: list[dict] = []
    covered_pts = 0.0
    weighted = 0.0
    for key in ("business", "financial", "market", "technical", "risk", "valuation"):
        max_pts = float(CATEGORY_WEIGHTS[key])
        if key in categories:
            cat = categories[key]
            cov = cat.coverage()
            score10 = round(cat.score10(), 1) if cov > 0 else None
            if score10 is not None:
                covered_pts += max_pts
                weighted += max_pts * score10
            rows.append({
                "key": key, "label": _QUICK_LABEL[key], "max_points": max_pts,
                "score10": score10, "points": round(cat.points(), 2),
                "coverage": round(cov, 2), "status": "scored",
            })
        else:
            rows.append({
                "key": key, "label": _QUICK_LABEL[key], "max_points": max_pts,
                "score10": None, "points": None, "coverage": 0.0,
                "status": "not_scorable",
                "reason": _ns_reason(key, packet.get("fmp_status")),
            })

    overall = round(weighted / covered_pts, 1) if covered_pts else None
    return {
        "categories": rows,
        "overall_10": overall,
        "evidence_points_covered": int(covered_pts),
        "evidence_points_total": 100,
        "disclaimer": (
            "Quick scorecard from SEC EDGAR fundamentals + FMP market data "
            f"(price/estimates); {int(covered_pts)}/100 evidence points covered. "
            "Not the full methodology."
        ),
    }
