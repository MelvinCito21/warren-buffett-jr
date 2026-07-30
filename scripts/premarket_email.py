#!/usr/bin/env python3
"""Pre-Market Movers email — corre en GitHub Actions cada mañana de mercado.

Usage:
    RESEND_API_KEY=... python3 scripts/premarket_email.py
    DRY_RUN=1 FORCE=1 python3 scripts/premarket_email.py   # prueba local sin enviar

Env vars:
    RESEND_API_KEY  clave de https://resend.com (requerida salvo DRY_RUN=1)
    EMAIL_TO        destinatario (default: victor@infusioninvestments.com)
    EMAIL_FROM      remitente   (default: onboarding@resend.dev — solo puede
                    enviar al email dueño de la cuenta Resend; verifica tu
                    dominio en Resend para usar otro remitente)
    FORCE=1         salta el chequeo de hora/feriado (para pruebas y
                    workflow_dispatch)
    DRY_RUN=1       imprime el email en stdout en vez de enviarlo

Stdlib only — sin dependencias.
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# Recipient comes from the environment (a GitHub secret in production), not
# hardcoded — this file is public and an email address in public source is
# personal data that gets scraped for spam. No default: if it isn't set the
# send aborts loudly rather than mailing an unintended address.
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Warren Buffett Jr <onboarding@resend.dev>")

GAINERS_URL = "https://stockanalysis.com/markets/premarket/"
LOSERS_URL = "https://stockanalysis.com/markets/premarket/losers/"

# FMP feeds the four added sections (news, earnings, macro, insiders). Read
# from the same env var the wbj engine uses. Without it those sections are
# skipped with a note — the email still sends the pre-market movers rather
# than failing, so a missing key degrades the email instead of breaking it.
FMP_API_KEY = os.environ.get("FMP_API_KEY")
FMP_BASE = "https://financialmodelingprep.com/stable"

# Insider filter: only open-market purchases (code P) above this size. A
# purchase is the one insider action with a single explanation; sales,
# awards, gifts and option exercises are excluded (see the wbj EDGAR
# reader for why the codes are not interchangeable).
INSIDER_MIN_USD = 1_000_000.0

# Feriados NYSE/Nasdaq (mercado cerrado). Actualizar cada año.
MARKET_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

MESES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
         "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

LARGE_CAP_MIN = 10e9  # $10B+ = "lo más importante"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_market_cap(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([TBM]?)", s.replace(",", ""))
    if not m:
        return 0.0
    mult = {"T": 1e12, "B": 1e9, "M": 1e6, "": 1.0}[m.group(2)]
    return float(m.group(1)) * mult


def parse_movers(page: str, limit: int = 10) -> list[dict]:
    """Parsea la tabla SSR de stockanalysis.com (celdas: #, ticker, nombre,
    % cambio, precio, ..., market cap al final)."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)  # ruido de Svelte
    body = re.search(r"<tbody>(.*?)</tbody>", page, flags=re.S)
    if not body:
        return []
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), flags=re.S)[:limit]:
        tds = [html.unescape(re.sub(r"<[^>]+>", "", td)).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
        if len(tds) < 5:
            continue
        try:
            rows.append({
                "ticker": tds[1],
                "name": tds[2],
                "pct": float(tds[3].replace("%", "").replace(",", "")),
                "price": tds[4],
                "mcap": parse_market_cap(tds[-1]),
            })
        except ValueError:
            continue
    return rows


def fmt_pct(p: float) -> str:
    return f"{'+' if p > 0 else '−'}{abs(p):.1f}%"


def table_html(rows: list[dict], color: str) -> str:
    tr = ""
    for r in rows:
        tr += (
            f'<tr style="border-top:1px solid #eee;">'
            f'<td style="padding:8px;font-weight:700;">{html.escape(r["ticker"])}</td>'
            f'<td style="padding:8px;">{html.escape(r["name"])}</td>'
            f'<td style="padding:8px;color:{color};font-weight:700;">{fmt_pct(r["pct"])}</td>'
            f'<td style="padding:8px;">${r["price"]}</td></tr>'
        )
    return f'<table style="width:100%;border-collapse:collapse;font-size:14px;">{tr}</table>'


# ============================================================================
# FMP sections (news / earnings / macro / insiders) — stdlib urllib only
# ============================================================================


def fmp_get(path: str, **params) -> list:
    """GET an FMP /stable endpoint, returning a list ([] on any failure).

    Never raises: a section that cannot fetch renders empty rather than
    aborting the whole email. Requires FMP_API_KEY; returns [] without it.
    """
    if not FMP_API_KEY:
        return []
    params["apikey"] = FMP_API_KEY
    url = f"{FMP_BASE}/{path}?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wbj-daily/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001 — degrade, never break the email
        print(f"FMP {path} falló: {e}", file=sys.stderr)
        return []


def get_news(limit: int = 6) -> list[dict]:
    """Latest general market headlines. Titles stay in their source
    language (English) — Melvin asked for the news untranslated."""
    rows = fmp_get("news/general-latest", page=0, limit=25)
    out = []
    for r in rows[:limit]:
        out.append({
            "title": r.get("title", "").strip(),
            "publisher": r.get("publisher") or r.get("site") or "",
            "url": r.get("url", ""),
        })
    return [r for r in out if r["title"]]


def get_earnings_today(now: datetime, limit: int = 8) -> list[dict]:
    """Companies reporting earnings today, biggest revenue estimate first.

    Revenue estimate is a rough size proxy so the household names lead and
    the micro-caps fall off the bottom."""
    today = now.strftime("%Y-%m-%d")
    rows = fmp_get("earnings-calendar", **{"from": today, "to": today})
    dated = [r for r in rows if r.get("date") == today]
    dated.sort(key=lambda r: r.get("revenueEstimated") or 0, reverse=True)
    out, seen = [], set()
    for r in dated:
        symbol = r.get("symbol", "")
        # FMP repeats a name across share classes and duplicate rows
        # (GOOG appeared three times); one line per ticker is enough.
        if not symbol or symbol in seen:
            continue
        # Preferred shares and baby bonds (T-PC, T-PA, TBB) carry the
        # common's EPS but aren't real earnings events. Drop the obvious
        # non-common patterns: a hyphen/dot suffix, or a lone trailing
        # letter after a hyphen.
        if "-" in symbol or "." in symbol:
            continue
        seen.add(symbol)
        out.append({
            "symbol": symbol,
            "eps_est": r.get("epsEstimated"),
            "rev_est": r.get("revenueEstimated"),
        })
        if len(out) >= limit:
            break
    return out


def get_macro_today(now: datetime, limit: int = 8) -> list[dict]:
    """Today's US economic releases that markets actually watch.

    Restricted to US and to High/Medium impact — the low-impact rows are
    noise that would bury the CPI/Fed/jobs prints that move the open."""
    today = now.strftime("%Y-%m-%d")
    rows = fmp_get("economic-calendar", **{"from": today, "to": today})
    keep = []
    for r in rows:
        country = (r.get("country") or "").upper()
        impact = (r.get("impact") or "").capitalize()
        if country not in ("US", "USA") or impact not in ("High", "Medium"):
            continue
        if not (r.get("date") or "").startswith(today):
            continue
        keep.append({
            "event": r.get("event", ""),
            "impact": impact,
            "estimate": r.get("estimate"),
            "previous": r.get("previous"),
            "unit": r.get("unit") or "",
        })
    order = {"High": 0, "Medium": 1}
    keep.sort(key=lambda r: order.get(r["impact"], 9))
    return keep[:limit]


def get_insider_buys(limit: int = 6, pages: int = 4) -> list[dict]:
    """Recent open-market insider PURCHASES above INSIDER_MIN_USD, market-wide.

    Only code-P purchases: an insider buys for one reason, so a purchase
    carries signal a sale does not. Sales, awards, gifts and option
    exercises are all excluded here. Pages a few times because purchases
    of this size are rare and the latest feed is mostly awards and sales.
    """
    seen, out = set(), []
    for page in range(pages):
        rows = fmp_get("insider-trading/latest", page=page, limit=100)
        if not rows:
            break
        for r in rows:
            ttype = (r.get("transactionType") or "").upper()
            if not ttype.startswith("P"):  # P-Purchase only
                continue
            if (r.get("acquisitionOrDisposition") or "").upper() != "A":
                continue
            shares = r.get("securitiesTransacted") or 0
            price = r.get("price") or 0
            value = shares * price
            if value < INSIDER_MIN_USD:
                continue
            dedup = (r.get("symbol"), r.get("reportingName"),
                     r.get("transactionDate"), shares, price)
            if dedup in seen:
                continue
            seen.add(dedup)
            role = (r.get("typeOfOwner") or "").strip(" :,").strip()
            out.append({
                "symbol": r.get("symbol", ""),
                "name": (r.get("reportingName") or "").title(),
                "title": role or "insider",
                "value": value,
                "date": r.get("transactionDate") or r.get("filingDate"),
            })
    out.sort(key=lambda r: r["value"], reverse=True)
    return out[:limit]


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def build_email(now: datetime, gainers: list[dict], losers: list[dict]) -> tuple[str, str, str]:
    fecha = f"{DIAS[now.weekday()]} {now.day} {MESES[now.month]} {now.year}"
    subject = f"🌅 Resumen de Mercado — {fecha}"

    big = sorted([r for r in gainers + losers if r["mcap"] >= LARGE_CAP_MIN],
                 key=lambda r: -abs(r["pct"]))[:6]
    small_g = [r for r in gainers if r["mcap"] < LARGE_CAP_MIN][:5]
    small_l = [r for r in losers if r["mcap"] < LARGE_CAP_MIN][:5]

    # The four FMP-backed sections. Each degrades to an empty list on any
    # failure, so the email always sends the movers even if FMP is down.
    news = get_news()
    earnings = get_earnings_today(now)
    macro = get_macro_today(now)
    insiders = get_insider_buys()

    def txt_rows(rows):
        return "\n".join(f"- {r['ticker']} {r['name']}: {fmt_pct(r['pct'])} a ${r['price']}"
                         for r in rows)

    def txt_eps(v):
        return f"EPS est. {v:.2f}" if isinstance(v, (int, float)) else "sin estimado"

    macro_txt = "\n".join(
        f"- [{m['impact']}] {m['event']}"
        + (f" — est. {m['estimate']}{m['unit']}" if m['estimate'] is not None else "")
        for m in macro
    ) or "- (sin datos macro de EE.UU. de alto impacto hoy)"

    earnings_txt = "\n".join(
        f"- {e['symbol']}: {txt_eps(e['eps_est'])}" for e in earnings
    ) or "- (ninguna empresa relevante reporta hoy)"

    insiders_txt = "\n".join(
        f"- {i['symbol']} — {i['name']} ({i['title']}): compró {fmt_usd(i['value'])} el {i['date']}"
        for i in insiders
    ) or "- (ninguna compra de insider > $1M en las últimas horas)"

    news_txt = "\n".join(
        f"- {n['title']} ({n['publisher']})" for n in news
    ) or "- (sin titulares disponibles)"

    text = f"""RESUMEN DIARIO DE MERCADO — {fecha}
({now.strftime('%H:%M')} ET)

════ NOTICIAS DEL MERCADO (en inglés) ════
{news_txt}

════ DATOS ECONÓMICOS DE HOY (EE.UU.) ════
{macro_txt}

════ EARNINGS DE HOY ════
{earnings_txt}

════ INSIDER BUYING > $1M (mercado completo) ════
{insiders_txt}

════ PRE-MARKET — LO MÁS IMPORTANTE (large caps $10B+) ════
{txt_rows(big) or '- (ninguna large cap con movimiento fuerte hoy)'}

════ GANADORES PRE-MARKET (small caps) ════
{txt_rows(small_g)}

════ PERDEDORES PRE-MARKET ════
{txt_rows(small_l)}

---
Clasificación de research — no es asesoría de inversión ni recomendación de compra/venta.
Warren Buffett Jr 🎩📈
"""

    big_html = (table_html(big, "#e17055") if big else
                '<p style="font-size:13px;color:#888;">Ninguna large cap con movimiento fuerte hoy.</p>')

    htmlbody = f"""<div style="background:#eef0f6;padding:16px 0;">
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:0 auto;color:#1a1a2e;background:#ffffff;border-radius:12px;">
  <div style="background:#6c5ce7;color:#fff;padding:20px 24px;border-radius:12px 12px 0 0;">
    <div style="font-size:12px;letter-spacing:2px;opacity:.85;">WARREN BUFFETT JR · MOTOR DE ANÁLISIS</div>
    <h1 style="margin:6px 0 0;font-size:22px;color:#ffffff;">🌅 Resumen de Mercado — {fecha}</h1>
    <div style="font-size:13px;opacity:.85;margin-top:4px;">{now.strftime('%H:%M')} ET · noticias, macro, earnings, insiders y pre-market</div>
  </div>
  <div style="background:#ffffff;border:1px solid #e5e5f0;border-top:none;padding:20px 24px;border-radius:0 0 12px 12px;">

    <h2 style="font-size:15px;margin:0 0 10px;color:#6c5ce7;">📰 Noticias del mercado <span style="font-size:11px;color:#999;font-weight:400;">(en inglés)</span></h2>
    {news_html(news)}

    <h2 style="font-size:15px;margin:24px 0 10px;color:#0984e3;">🏛️ Datos económicos de hoy — EE.UU.</h2>
    {macro_html(macro)}

    <h2 style="font-size:15px;margin:24px 0 10px;color:#6c5ce7;">📅 Earnings de hoy</h2>
    {earnings_html(earnings)}

    <h2 style="font-size:15px;margin:24px 0 10px;color:#00b894;">💰 Insider buying &gt; $1M <span style="font-size:11px;color:#999;font-weight:400;">(compras en mercado abierto, todo el mercado)</span></h2>
    {insiders_html(insiders)}

    <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">

    <h2 style="font-size:15px;margin:0 0 10px;color:#e17055;">🔥 Pre-market — lo más importante (large caps $10B+)</h2>
    {big_html}
    <h2 style="font-size:15px;margin:22px 0 10px;color:#00b894;">🚀 Ganadores pre-market (small caps)</h2>
    {table_html(small_g, "#00b894")}
    <h2 style="font-size:15px;margin:22px 0 10px;color:#d63031;">📉 Perdedores pre-market</h2>
    {table_html(small_l, "#d63031")}

    <hr style="border:none;border-top:1px solid #eee;margin:16px 0;">
    <p style="font-size:11px;color:#aaa;margin:0;">Noticias en inglés (fuente sin traducir) · Macro/earnings/insiders vía FMP · Pre-market vía stockanalysis.com · Insider &gt; $1M = solo compras en mercado abierto (código P). Clasificación de research — no es asesoría de inversión ni recomendación de compra/venta. · Warren Buffett Jr 🎩📈</p>
  </div>
</div>
</div>"""
    return subject, text, htmlbody


# ============================================================================
# HTML renderers for the four sections
# ============================================================================


def _empty(msg: str) -> str:
    return f'<p style="font-size:13px;color:#999;margin:0;">{msg}</p>'


def news_html(news: list[dict]) -> str:
    if not news:
        return _empty("Sin titulares disponibles ahora.")
    items = ""
    for n in news:
        items += (
            f'<li style="margin:0 0 8px;font-size:14px;line-height:1.4;">'
            f'<a href="{html.escape(n["url"])}" style="color:#1a1a2e;text-decoration:none;">'
            f'{html.escape(n["title"])}</a>'
            f' <span style="color:#999;font-size:12px;">— {html.escape(n["publisher"])}</span></li>'
        )
    return f'<ul style="margin:0;padding-left:18px;">{items}</ul>'


def macro_html(macro: list[dict]) -> str:
    if not macro:
        return _empty("Sin datos macro de EE.UU. de alto impacto hoy.")
    rows = ""
    for m in macro:
        color = "#d63031" if m["impact"] == "High" else "#e17055"
        est = f'est. {m["estimate"]}{m["unit"]}' if m["estimate"] is not None else "—"
        prev = f'prev. {m["previous"]}{m["unit"]}' if m["previous"] is not None else ""
        rows += (
            f'<tr style="border-top:1px solid #eee;">'
            f'<td style="padding:7px 8px;"><span style="color:{color};font-weight:700;font-size:11px;">{m["impact"].upper()}</span></td>'
            f'<td style="padding:7px 8px;font-size:14px;">{html.escape(m["event"])}</td>'
            f'<td style="padding:7px 8px;font-size:13px;color:#555;white-space:nowrap;">{html.escape(est)} {html.escape(prev)}</td></tr>'
        )
    return f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'


def earnings_html(earnings: list[dict]) -> str:
    if not earnings:
        return _empty("Ninguna empresa relevante reporta hoy.")
    chips = ""
    for e in earnings:
        eps = (f'EPS est. {e["eps_est"]:.2f}'
               if isinstance(e["eps_est"], (int, float)) else "sin estimado")
        chips += (
            f'<span style="display:inline-block;margin:0 6px 6px 0;padding:5px 10px;'
            f'background:#f3f0ff;border-radius:6px;font-size:13px;">'
            f'<b>{html.escape(e["symbol"])}</b> '
            f'<span style="color:#777;">{html.escape(eps)}</span></span>'
        )
    return f'<div>{chips}</div>'


def insiders_html(insiders: list[dict]) -> str:
    if not insiders:
        return _empty("Ninguna compra de insider &gt; $1M en las últimas horas.")
    rows = ""
    for i in insiders:
        rows += (
            f'<tr style="border-top:1px solid #eee;">'
            f'<td style="padding:7px 8px;font-weight:700;">{html.escape(i["symbol"])}</td>'
            f'<td style="padding:7px 8px;font-size:14px;">{html.escape(i["name"])}'
            f'<span style="color:#999;font-size:12px;"> · {html.escape(i["title"])}</span></td>'
            f'<td style="padding:7px 8px;color:#00b894;font-weight:700;white-space:nowrap;">{fmt_usd(i["value"])}</td>'
            f'<td style="padding:7px 8px;color:#999;font-size:12px;white-space:nowrap;">{html.escape(str(i["date"]))}</td></tr>'
        )
    return f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'


def send_resend(subject: str, text: str, htmlbody: str) -> None:
    key = os.environ["RESEND_API_KEY"]
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "text": text,
        "html": htmlbody,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Cloudflare (in front of Resend) 403s the default
            # `Python-urllib` agent as a bot with error code 1010. A normal
            # User-Agent gets a legitimate, authenticated API call through.
            "User-Agent": "warren-buffett-jr/1.0 (+https://github.com)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"Resend: {r.status} {r.read().decode()}")
    except urllib.error.HTTPError as e:
        # urlopen raises before we can read the body, so Resend's actual
        # reason (invalid key, unverified sender, recipient restriction on
        # the shared onboarding@resend.dev address) is otherwise invisible.
        # Surfacing it turns a bare "403 Forbidden" into an actionable message.
        body = e.read().decode(errors="replace")
        print(f"ERROR Resend {e.code}: {body}", file=sys.stderr)
        raise


def main() -> int:
    now = datetime.now(ET)
    force = os.environ.get("FORCE") == "1"

    if not force:
        # El workflow corre 12:00 y 13:00 UTC; solo una equivale a las 8 ET.
        if now.hour != 8:
            print(f"Son las {now.strftime('%H:%M')} ET, no las 8 — skip (cron UTC/DST).")
            return 0
        if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:
            print("Mercado cerrado hoy — skip.")
            return 0

    gainers = parse_movers(fetch(GAINERS_URL))
    losers = parse_movers(fetch(LOSERS_URL))
    if not gainers and not losers:
        print("ERROR: no pude parsear movers (¿cambió el HTML de stockanalysis.com?)",
              file=sys.stderr)
        return 1

    subject, text, htmlbody = build_email(now, gainers, losers)

    if os.environ.get("DRY_RUN") == "1":
        print(f"[DRY RUN] to={EMAIL_TO or '(EMAIL_TO no configurado)'}\n"
              f"subject={subject}\n\n{text}")
        return 0
    if not EMAIL_TO:
        print("ERROR: EMAIL_TO no está configurado — configúralo como secret "
              "en GitHub Actions antes de enviar.", file=sys.stderr)
        return 1
    send_resend(subject, text, htmlbody)
    print(f"Enviado a {EMAIL_TO}: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
