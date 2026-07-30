"""SEC EDGAR provider: ticker->CIK lookup, XBRL company facts, filing metadata.

Tier-1 per Cerebro/shared/SOURCE_HIERARCHY.md ("Regulatory filing and filing
acceptance metadata" ranks first). No API key is required — `EdgarProvider`
is always `available`. SEC's fair-access policy requires a descriptive
`User-Agent` identifying the requester on every request
(https://www.sec.gov/os/webmaster-faq#developers); `EDGAR_USER_AGENT` is
sent on every call via `wbj.providers.base.Provider.get_json`'s `headers`
pass-through.

Endpoints:
- `https://www.sec.gov/files/company_tickers.json` — ticker -> CIK map,
  one global payload (not per-ticker), refreshed roughly monthly by SEC,
  so cached for up to 30 days under a fixed global cache entry.
- `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json` — all
  XBRL (dei/us-gaap/...) facts reported by the company across filings.
  Cached per-CIK for up to 1 day.
- `https://data.sec.gov/submissions/CIK{cik:010d}.json` — filing history
  including `acceptanceDateTime`, used to determine filing recency and to
  list Form 4 (insider) and 13F-HR (institutional) filings.
  Cached per-CIK for up to 1 day.
- `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}` —
  the filing document itself, as XML. Cached per-accession indefinitely:
  a filed document is immutable, so it never needs refetching.
"""

from __future__ import annotations

import re

from wbj.providers.base import Provider

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# `primaryDocument` points at the XSL-rendered HTML view of a filing
# (`xslF345X06/wk-form4_123.xml`), not the raw XML. Stripping the leading
# `xsl*/` segment yields the machine-readable document in the same
# directory, which is what the parsers need.
_XSL_PREFIX = re.compile(r"^xsl[^/]*/")

EDGAR_USER_AGENT = "warren-buffett-jr victor@infusioninvestments.com"
_EDGAR_HEADERS = {"User-Agent": EDGAR_USER_AGENT}

# The tickers map is one global, ticker-independent payload, so it is
# cached under a fixed pseudo-ticker rather than the caller's ticker —
# looking up a second ticker must reuse the same cache entry.
_GLOBAL_CACHE_TICKER = "_GLOBAL"

_MAX_AGE_TICKERS = 30
_MAX_AGE_COMPANYFACTS = 1
_MAX_AGE_SUBMISSIONS = 1


def _cik_cache_key(cik: int) -> str:
    return f"CIK{cik:010d}"


class EdgarProvider(Provider):
    """SEC EDGAR data provider (no API key required)."""

    @property
    def available(self) -> bool:
        """Always True — EDGAR requires no API key, only a User-Agent header."""
        return True

    def cik_for(self, ticker: str) -> int | None:
        """Look up the CIK for `ticker` via SEC's company_tickers.json map.

        Returns None if the ticker isn't found or the payload is malformed.
        """
        payload = self.get_json(
            TICKERS_URL,
            {},
            "tickers",
            _GLOBAL_CACHE_TICKER,
            max_age_days=_MAX_AGE_TICKERS,
            headers=_EDGAR_HEADERS,
        )
        if not isinstance(payload, dict):
            return None

        ticker_upper = ticker.upper()
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ticker", "")).upper() != ticker_upper:
                continue
            cik = entry.get("cik_str")
            try:
                return int(cik)
            except (TypeError, ValueError):
                return None
        return None

    def companyfacts(self, cik: int) -> dict | None:
        """Fetch all XBRL company facts (dei/us-gaap/...) for `cik`."""
        payload = self.get_json(
            COMPANYFACTS_URL.format(cik=cik),
            {},
            "companyfacts",
            _cik_cache_key(cik),
            max_age_days=_MAX_AGE_COMPANYFACTS,
            headers=_EDGAR_HEADERS,
        )
        return payload if isinstance(payload, dict) else None

    def _recent_filings(self, cik: int) -> dict | None:
        """Return the `filings.recent` column arrays for `cik`, or None.

        Note this covers only the most recent ~1,000 filings. SEC pages
        older history into `filings.files`, which is not followed — for a
        heavy filer like NVDA (566 Form 4s in `recent`) that is several
        years of insider activity, which is more than the report reads.
        """
        payload = self.get_json(
            SUBMISSIONS_URL.format(cik=cik),
            {},
            "submissions",
            _cik_cache_key(cik),
            max_age_days=_MAX_AGE_SUBMISSIONS,
            headers=_EDGAR_HEADERS,
        )
        if not isinstance(payload, dict):
            return None

        recent = payload.get("filings", {}).get("recent")
        return recent if isinstance(recent, dict) else None

    def filings_of_type(
        self, cik: int, form: str, limit: int = 40
    ) -> list[dict] | None:
        """List `cik`'s most recent filings of exactly `form`, newest first.

        `form` matches exactly, not by prefix: "4" must not pull in "4/A"
        (an amendment, which restates a transaction already counted) and
        "13F-HR" must not pull in "13F-HR/A" or "13F-NT" (a notice that the
        fund holds nothing reportable).

        Each entry carries `accessionNumber`, `filingDate` and `document`,
        the raw XML filename ready for `filing_document`. Returns None if
        the submissions payload is malformed.
        """
        recent = self._recent_filings(cik)
        if recent is None:
            return None

        rows = zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
            strict=False,
        )
        out = [
            {
                "accessionNumber": accession,
                "filingDate": filed,
                "document": _XSL_PREFIX.sub("", document or ""),
            }
            for form_type, filed, accession, document in rows
            if form_type == form
        ]
        return out[:limit]

    def filing_document(self, cik: int, accession: str, document: str) -> str | None:
        """Fetch one filing's raw XML text from the EDGAR archives.

        `accession` is the dashed accession number; the archive path wants
        it undashed. Cached without expiry — a filed document is immutable.
        """
        bare_accession = accession.replace("-", "")
        return self.get_text(
            ARCHIVES_URL.format(
                cik=cik, accession=bare_accession, document=document
            ),
            {},
            f"filing_{bare_accession}_{document}",
            _cik_cache_key(cik),
            max_age_days=None,
            headers=_EDGAR_HEADERS,
        )

    def filing_index(self, cik: int, accession: str) -> list[str] | None:
        """List the filenames inside one accession's archive directory.

        Form 4 does not need this — `primaryDocument` names its XML. A
        13F does: `primaryDocument` points at `primary_doc.xml`, the cover
        page, while the holdings live in a separate file whose name is
        assigned by the filing agent. Berkshire's is `53405.xml`;
        NVIDIA's is `information_table.xml`. Neither is predictable.
        """
        bare_accession = accession.replace("-", "")
        payload = self.get_json(
            ARCHIVES_URL.format(
                cik=cik, accession=bare_accession, document="index.json"
            ),
            {},
            f"index_{bare_accession}",
            _cik_cache_key(cik),
            max_age_days=None,
            headers=_EDGAR_HEADERS,
        )
        if not isinstance(payload, dict):
            return None
        items = payload.get("directory", {}).get("item")
        if not isinstance(items, list):
            return None
        return [str(item.get("name", "")) for item in items if isinstance(item, dict)]

    def information_table_document(self, cik: int, accession: str) -> str | None:
        """Name of the 13F holdings XML inside `accession`, or None.

        Picks the one `.xml` that is neither the cover page nor an XSL
        rendering. Returns None rather than guessing if the directory
        holds no such file — a 13F-NT (notice of no holdings) legitimately
        has none, and inventing one would fabricate a portfolio.
        """
        names = self.filing_index(cik, accession)
        if names is None:
            return None
        candidates = [
            name
            for name in names
            if name.lower().endswith(".xml")
            and "primary_doc" not in name.lower()
            and not name.lower().startswith("xsl")
        ]
        return candidates[0] if candidates else None

    def filing_acceptance_times(self, cik: int) -> list[dict] | None:
        """Return recent filings' form/acceptanceDateTime/accessionNumber.

        Derived from `https://data.sec.gov/submissions/CIK{cik}.json`'s
        `filings.recent` arrays. Returns None if the payload is malformed
        or lacks the expected `filings.recent` structure.
        """
        recent = self._recent_filings(cik)
        if recent is None:
            return None

        forms = recent.get("form", [])
        accept_times = recent.get("acceptanceDateTime", [])
        accession_numbers = recent.get("accessionNumber", [])

        return [
            {
                "form": form,
                "acceptanceDateTime": accepted,
                "accessionNumber": accession,
            }
            for form, accepted, accession in zip(
                forms, accept_times, accession_numbers, strict=False
            )
        ]
