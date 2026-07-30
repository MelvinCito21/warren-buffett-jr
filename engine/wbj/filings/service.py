"""Assemble EDGAR filings into report-ready structures.

The parsers in this package are pure functions over XML. This module is
the part that talks to `EdgarProvider`, walks the filings, and hands the
report plain JSON-serializable dicts with Spanish labels already applied.

Everything here degrades rather than raises. A ticker whose filings
cannot be read produces a result that says so — `disponible: False` with
a reason — because the report distinguishes "no insider bought" from "the
insider data could not be read", and those must never look alike.
"""

from __future__ import annotations

from typing import Any

from wbj.filings.form4 import parse_form4
from wbj.filings.insiders import summarize_insiders
from wbj.filings.superinvestors import (
    TRACKED_FUNDS,
    find_cusip,
    scan_superinvestors,
)
from wbj.filings.thirteenf import parse_13f

# How many Form 4 filings back to read. Sized by the heaviest filers, not
# the average: NVDA posts one per insider per event, so 40 filings covered
# only ~4 months where FMP's feed reached 7. Reading a narrower window and
# reporting a smaller sell total would look like insiders selling less,
# when it is the same selling seen over less time — a subtler error than
# the one being fixed. 120 restores roughly a year for NVDA and is far
# more than a year for a normal filer.
#
# The cost is one request per filing on a cold cache. Filed documents are
# immutable, so `filing_document` caches them without expiry and a second
# run of the same ticker costs nothing.
DEFAULT_FORM4_LIMIT = 120

# Two quarters per fund: the latest position and the one to compare it to.
QUARTERS_PER_FUND = 2


def insider_activity(
    edgar: Any, cik: int, limit: int = DEFAULT_FORM4_LIMIT
) -> dict:
    """Read `cik`'s recent Form 4 filings into a report-ready summary."""
    filings = edgar.filings_of_type(cik, "4", limit=limit)
    if filings is None:
        return {
            "disponible": False,
            "razon": "No se pudo leer el historial de filings de EDGAR.",
        }
    if not filings:
        return {
            "disponible": True,
            "razon": "El emisor no tiene Form 4 recientes en EDGAR.",
            "operaciones_totales": 0,
            "compras": [],
            "ventas": [],
            "por_persona": [],
        }

    transactions = []
    unreadable = 0
    for filing in filings:
        xml = edgar.filing_document(
            cik, filing["accessionNumber"], filing["document"]
        )
        if not xml:
            unreadable += 1
            continue
        transactions += parse_form4(
            xml, filing["accessionNumber"], filing["filingDate"]
        )

    summary = summarize_insiders(transactions)
    return {
        "disponible": True,
        "filings_leidos": len(filings) - unreadable,
        "filings_ilegibles": unreadable,
        "desde": filings[-1]["filingDate"],
        "hasta": filings[0]["filingDate"],
        "umbral_usd": summary.threshold_usd,
        "operaciones_totales": len(transactions),
        "descartadas_por_umbral_o_ruido": summary.excluded_count,
        "compra_usd": summary.purchase_usd,
        "venta_usd": summary.sale_usd,
        "ventas_programadas_10b5_1": summary.scheduled_sale_count,
        # Same filings, no size threshold — the buy-vs-sell bar's numbers.
        "flujo": summary.flow,
        "compras": [_tx_dict(t) for t in summary.purchases],
        "ventas": [_tx_dict(t) for t in summary.sales],
        "por_persona": summary.by_person,
        "nota": (
            "Solo operaciones de mercado abierto (códigos P y S) por más de "
            f"${summary.threshold_usd:,.0f}. Concesiones, ejercicios de "
            "opciones, regalos y retenciones por impuestos quedan fuera: no "
            "son decisiones de compra ni de venta."
        ),
    }


def _tx_dict(t) -> dict:
    return {
        "persona": t.owner_name,
        "cargo": t.owner_title,
        "fecha": t.date,
        "lado": t.side_es,
        "codigo": t.code,
        "codigo_label": t.spec.label_es,
        "acciones": t.shares,
        "precio": t.price,
        "valor_usd": t.value_usd,
        "programada_10b5_1": t.is_10b5_1,
        "accession": t.accession,
    }


def _latest_13f_filings(edgar: Any, cik: int, quarters: int) -> list:
    """Parse a fund's most recent `quarters` 13F-HR filings, newest first."""
    filings = edgar.filings_of_type(cik, "13F-HR", limit=quarters)
    if not filings:
        return []

    parsed = []
    for filing in filings:
        document = edgar.information_table_document(
            cik, filing["accessionNumber"]
        )
        if not document:
            continue
        xml = edgar.filing_document(cik, filing["accessionNumber"], document)
        if not xml:
            continue
        parsed.append(
            parse_13f(
                xml,
                fund_name=TRACKED_FUNDS.get(cik, ""),
                fund_cik=cik,
                filing_date=filing["filingDate"],
                period_of_report=_period_from_filing_date(filing["filingDate"]),
            )
        )
    return parsed


def _period_from_filing_date(filing_date: str) -> str:
    """Quarter-end covered by a filing submitted on `filing_date`.

    The cover page (`primary_doc.xml`) states the period authoritatively,
    but fetching it doubles the request count for a value that follows
    mechanically from the filing month. 13Fs are due 45 days after quarter
    end, so a February filing reports December, May reports March, and so
    on. Returns "" for a date that fits no window rather than guessing.
    """
    try:
        year, month, _ = (int(part) for part in filing_date.split("-"))
    except (ValueError, AttributeError):
        return ""
    if month in (1, 2, 3):
        return f"{year - 1}-12-31"
    if month in (4, 5, 6):
        return f"{year}-03-31"
    if month in (7, 8, 9):
        return f"{year}-06-30"
    return f"{year}-09-30"


def superinvestor_positions(
    edgar: Any, issuer_hint: str, cusip: str | None = None
) -> dict:
    """Scan the tracked funds for a position in `issuer_hint`.

    `issuer_hint` is matched against 13F issuer names only to discover the
    CUSIP; pass `cusip` directly once it is known to skip that step.
    """
    current: dict[int, Any] = {}
    previous: dict[int, Any] = {}

    for fund_cik in TRACKED_FUNDS:
        parsed = _latest_13f_filings(edgar, fund_cik, QUARTERS_PER_FUND)
        if not parsed:
            continue
        current[fund_cik] = parsed[0]
        if len(parsed) > 1:
            previous[fund_cik] = parsed[1]

    if not current:
        return {
            "disponible": False,
            "razon": "No se pudo leer ningún 13F de los fondos seguidos.",
        }

    resolved = cusip or find_cusip(list(current.values()), issuer_hint)
    if not resolved:
        return {
            "disponible": True,
            "cusip": None,
            "posiciones": [],
            "nota": (
                f"Ningún fondo de la lista reporta a {issuer_hint!r}, así que "
                "no se pudo identificar su CUSIP desde los 13F leídos. Eso no "
                "descarta que otros inversionistas la tengan."
            ),
        }

    scan = scan_superinvestors(resolved, current, previous)
    return {
        "disponible": True,
        "cusip": scan.cusip,
        "fondos_revisados": scan.funds_scanned,
        "fondos_sin_data": scan.funds_unavailable,
        "posiciones": [
            {
                "fondo": p.fund_name,
                "cik": p.fund_cik,
                "acciones": p.holding.shares,
                "valor_usd": p.holding.value_usd,
                "filas_en_el_filing": p.holding.row_count,
                "cambio": p.change_es,
                "cambio_pct": p.change_pct,
                "fecha_filing": p.filing_date,
                "periodo": p.period_of_report,
                "nota_antiguedad": p.staleness_note_es,
            }
            for p in scan.positions
        ],
        "nota_cobertura": scan.coverage_note_es(),
        "limites": [
            "Los 13F llegan con hasta 45 días de retraso: no es la posición de hoy.",
            "Solo muestran posiciones largas en acciones de EE.UU.; las apuestas "
            "en contra no aparecen.",
            "Es un hecho, no una recomendación.",
        ],
    }
