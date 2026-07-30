"""Check every CIK in TRACKED_FUNDS still points at the manager it claims.

A CIK is just a number, and a wrong one fails silently in the worst
possible way: the report attributes one manager's positions to another.
CIK 1637460 reads perfectly plausibly as Michael Burry's Scion; it is
Man Group plc.

This hits the network, so it is a script rather than a test. Run it when
adding a fund, and occasionally after — managers do re-register under new
entities (Duquesne Capital closed in 2010; the family office files under
a different CIK entirely).

    .venv/bin/python scripts/verify_fund_ciks.py
"""

from __future__ import annotations

import sys

from wbj.filings.superinvestors import TRACKED_FUNDS_META
from wbj.providers.cache import Cache
from wbj.providers.edgar import (
    _EDGAR_HEADERS,
    SUBMISSIONS_URL,
    EdgarProvider,
)


def main() -> int:
    provider = EdgarProvider(settings=None, cache=Cache("cache"))
    problems = 0

    for cik, meta in sorted(TRACKED_FUNDS_META.items()):
        payload = provider.get_json(
            SUBMISSIONS_URL.format(cik=cik),
            {},
            "submissions",
            f"CIK{cik:010d}",
            max_age_days=1,
            headers=_EDGAR_HEADERS,
        )
        if not payload:
            print(f"FALLA  {cik:>10}  {meta['label']}: EDGAR no devolvió nada")
            problems += 1
            continue

        actual = payload.get("name", "")
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        count = sum(1 for f in forms if f == "13F-HR")
        last = next((d for f, d in zip(forms, dates) if f == "13F-HR"), "ninguno")

        if actual != meta["edgar_name"]:
            print(
                f"FALLA  {cik:>10}  esperaba {meta['edgar_name']!r}, "
                f"EDGAR dice {actual!r}"
            )
            problems += 1
            continue
        if count == 0:
            print(f"AVISO  {cik:>10}  {actual}: sin 13F-HR en el historial reciente")
            problems += 1
            continue

        print(f"ok     {cik:>10}  {actual:<46} 13F-HR={count:<4} último={last}")

    print()
    if problems:
        print(f"{problems} problema(s). Corrige TRACKED_FUNDS_META antes de confiar en el reporte.")
    else:
        print(f"Los {len(TRACKED_FUNDS_META)} CIKs coinciden con lo que dice EDGAR.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
