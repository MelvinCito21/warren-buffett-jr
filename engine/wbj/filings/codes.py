"""SEC Form 4 transaction codes and what each one actually means.

Downloading a Form 4 is trivial; reading it is not. The filing reports a
one-letter transaction code, and the codes do not mean remotely the same
thing. A real NVDA filing (accession 0001197647-26-000005) has director
Tench Coxe moving 500,000 shares at $0.00 under code `G` — a gift to a
family trust. A parser that only looks at the acquired/disposed flag
reports "director disposed of 500,000 shares" and implies he is fleeing
the stock. He is not; no shares were sold and no price was paid.

So codes are classified here, once, by what they say about conviction:

- `OPEN_MARKET_PURCHASE` (P) is the strong signal. An insider buys with
  their own money for one reason only.
- `OPEN_MARKET_SALE` (S) is a weak signal. Diversification, a house, a
  divorce, taxes — a sale has many innocent explanations that a purchase
  does not. Per `business-analysis.md`, buy and sell are not symmetric in
  meaning and must not be weighed as if they were.
- Everything else is not a conviction signal at all. Awards (A) are
  compensation the insider did not choose to buy. Option exercises (M)
  are the classic mistake — they look like an acquisition but the insider
  is exercising a right granted years ago, usually selling the same day.
  Tax withholding (F) is bookkeeping. Gifts (G) move shares without
  either side paying.

Only `is_conviction_signal` codes belong in the report's insider section.
The rest are reported, if at all, as context — never as "the CFO bought".

Source: SEC Form 4 instructions, Table I/II transaction code legend.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Meaning(Enum):
    """What a transaction code says about insider conviction."""

    OPEN_MARKET_PURCHASE = "open_market_purchase"
    OPEN_MARKET_SALE = "open_market_sale"
    OPTION_EXERCISE = "option_exercise"
    AWARD = "award"
    GIFT = "gift"
    TAX_WITHHOLDING = "tax_withholding"
    DERIVATIVE = "derivative"
    OTHER = "other"


@dataclass(frozen=True)
class CodeSpec:
    """One Form 4 transaction code and how the report must treat it."""

    code: str
    meaning: Meaning
    label_es: str
    is_conviction_signal: bool
    note_es: str = ""


def _spec(code, meaning, label_es, signal=False, note_es=""):
    return CodeSpec(code, meaning, label_es, signal, note_es)


# The full legend. Codes absent from this table fall back to UNKNOWN_CODE
# rather than being guessed at — an unrecognized code is reported as
# unrecognized, never silently folded into "sale".
TRANSACTION_CODES: dict[str, CodeSpec] = {
    # --- general transactions --------------------------------------------
    "P": _spec(
        "P", Meaning.OPEN_MARKET_PURCHASE, "Compra en mercado abierto",
        signal=True,
        note_es="Señal fuerte: el insider compró con su propio dinero.",
    ),
    "S": _spec(
        "S", Meaning.OPEN_MARKET_SALE, "Venta en mercado abierto",
        signal=True,
        note_es="Señal débil: una venta tiene muchas explicaciones inocentes.",
    ),
    "V": _spec(
        "V", Meaning.OTHER, "Reporte voluntario anticipado",
        note_es="Indica el momento del reporte, no el tipo de operación.",
    ),
    # --- Rule 16b-3 transactions -----------------------------------------
    "A": _spec(
        "A", Meaning.AWARD, "Concesión o premio",
        note_es="Compensación, no convicción: el insider no eligió comprar.",
    ),
    "D": _spec(
        "D", Meaning.OTHER, "Disposición hacia el emisor",
        note_es="Devolución de acciones a la empresa, no venta en mercado.",
    ),
    "F": _spec(
        "F", Meaning.TAX_WITHHOLDING, "Retención para impuestos",
        note_es="Ruido: acciones retenidas para pagar impuestos del vesting.",
    ),
    "I": _spec("I", Meaning.OTHER, "Transacción discrecional"),
    "M": _spec(
        "M", Meaning.OPTION_EXERCISE, "Ejercicio de opciones",
        note_es=(
            "NO es una compra. Ejerce un derecho concedido años atrás; "
            "suele venderse el mismo día."
        ),
    ),
    # --- derivative securities -------------------------------------------
    "C": _spec("C", Meaning.DERIVATIVE, "Conversión de derivado"),
    "E": _spec("E", Meaning.DERIVATIVE, "Expiración de posición corta en derivado"),
    "H": _spec("H", Meaning.DERIVATIVE, "Expiración de posición larga con valor recibido"),
    "O": _spec("O", Meaning.DERIVATIVE, "Ejercicio de derivado fuera del dinero"),
    "X": _spec("X", Meaning.DERIVATIVE, "Ejercicio de derivado dentro del dinero"),
    "K": _spec("K", Meaning.DERIVATIVE, "Equity swap o similar"),
    # --- other transactions ----------------------------------------------
    "G": _spec(
        "G", Meaning.GIFT, "Regalo",
        note_es=(
            "Ni compra ni venta: nadie pagó un precio. Suele ir a un "
            "fideicomiso familiar o a obra benéfica."
        ),
    ),
    "L": _spec("L", Meaning.OTHER, "Adquisición pequeña"),
    "W": _spec("W", Meaning.OTHER, "Adquisición o disposición por herencia"),
    "Z": _spec("Z", Meaning.OTHER, "Depósito o retiro de voting trust"),
    "J": _spec(
        "J", Meaning.OTHER, "Otra adquisición o disposición",
        note_es="Requiere leer la nota al pie del filing: el código no lo dice.",
    ),
    "U": _spec("U", Meaning.OTHER, "Disposición por cambio de control"),
}

UNKNOWN_CODE = _spec(
    "?", Meaning.OTHER, "Código no reconocido",
    note_es="No está en la tabla de la SEC que conoce el sistema.",
)


def spec_for(code: str | None) -> CodeSpec:
    """Return the CodeSpec for `code`, or UNKNOWN_CODE if unrecognized."""
    if not code:
        return UNKNOWN_CODE
    return TRANSACTION_CODES.get(code.strip().upper(), UNKNOWN_CODE)
