"""The judges' page: server-rendered HTML and inline SVG, no build step.

Fragments are rendered here and swapped in by a few lines of fetch-polling, so
every chart is a plain GET a test can assert on and a human can curl. There is
no charting library, no bundler, and nothing to go wrong on stage that cannot be
seen in the page source.

Colour follows the dataviz method: two categorical slots (router = blue slot 1,
baseline = orange slot 2), validated with the skill's checker in both modes —
worst-pair CVD ΔE 24.7 light / 26.8 dark against an ≥8 target, all six checks
PASS. Status colours are the reserved status palette and never used for a
series. Both themes are selected, not flipped.
"""

from __future__ import annotations

import html
from typing import Any

# --- palette (validated; see module docstring) -------------------------------

STYLE = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --surface-2: #f4f3f0; --line: #e6e4df;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #7c7a74;
  --router: #2a78d6; --baseline: #eb6834;
  --rail-1: #2a78d6; --rail-2: #eb6834; --rail-3: #1baf7a;
  --good: #0ca30c; --warn: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --real: #4a3aa7;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface: #1a1a19; --surface-2: #242423; --line: #383835;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8e8c84;
    --router: #3987e5; --baseline: #d95926;
    --rail-1: #3987e5; --rail-2: #d95926; --rail-3: #199e70;
    --real: #9085e9;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --surface-2: #242423; --line: #383835;
  --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8e8c84;
  --router: #3987e5; --baseline: #d95926;
  --rail-1: #3987e5; --rail-2: #d95926; --rail-3: #199e70;
  --real: #9085e9;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--surface); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
header { max-width: 1180px; margin: 0 auto 20px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--ink-2); font-size: 13px; margin: 0; }
main { max-width: 1180px; margin: 0 auto; display: grid; gap: 16px; }
.card {
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 10px; padding: 16px 18px;
}
.card h2 {
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--ink-2); margin: 0 0 12px; font-weight: 600;
}
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat { background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; color: var(--ink-3); }
.stat .value { font-size: 27px; font-variant-numeric: tabular-nums; margin-top: 4px; letter-spacing: -0.02em; }
.stat .note { font-size: 11px; color: var(--ink-3); margin-top: 3px; font-variant-numeric: tabular-nums; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--line); border-radius: 9px; padding: 12px 14px; background: var(--surface); }
.tile .name { font-weight: 600; font-size: 14px; }
.tile .meta { font-size: 12px; color: var(--ink-2); margin-top: 6px; font-variant-numeric: tabular-nums; }
.badge {
  display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
  padding: 2px 6px; border-radius: 4px; border: 1px solid currentColor; margin-left: 6px;
  vertical-align: 1px;
}
.badge.real { color: var(--real); }
.badge.sim { color: var(--ink-3); }
.state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.s-healthy { color: var(--good); } .s-healthy .dot { background: var(--good); }
.s-degraded { color: var(--serious); } .s-degraded .dot { background: var(--serious); }
.s-down { color: var(--critical); } .s-down .dot { background: var(--critical); }
.s-unknown { color: var(--ink-3); } .s-unknown .dot { background: var(--ink-3); }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--ink-2); margin-bottom: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-3); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--line);
}
td { padding: 7px 8px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
.chain { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--ink-2); }
.ok { color: var(--good); } .no { color: var(--critical); }
.empty { color: var(--ink-3); font-size: 13px; padding: 8px 0; }
.scroll { overflow-x: auto; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 860px) { .two { grid-template-columns: 1fr; } }
.strip { display: flex; flex-wrap: wrap; gap: 10px; }
.merchant { border: 1px solid var(--line); border-radius: 8px; padding: 9px 12px; background: var(--surface); }
.merchant .who { font-weight: 600; font-size: 13px; }
.merchant .num { font-size: 12px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.bar { height: 6px; border-radius: 3px; background: var(--router); display: inline-block; vertical-align: middle; }
footer { max-width: 1180px; margin: 20px auto 0; color: var(--ink-3); font-size: 12px; }
"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PayOptimize AI — live payment routing</title>
<style>{style}</style></head>
<body>
<header>
  <h1>PayOptimize AI</h1>
  <p class="sub">Every payment routed to the provider most likely to authorize it &mdash;
  and the routing is learned from outcomes, live. One real rail (Prava sandbox),
  three clearly-labeled simulated rails.</p>
</header>
<main>
  <div id="stats">{stats}</div>
  <section class="card"><h2>Authorization rate &mdash; router vs baseline</h2>
    <div id="authchart">{authchart}</div></section>
  <section class="card"><h2>Where the router sent first attempts</h2>
    <div id="mixchart">{mixchart}</div></section>
  <section class="card"><h2>Provider health</h2><div id="tiles">{tiles}</div></section>
  <div class="two">
    <section class="card"><h2>By corridor</h2><div class="scroll" id="corridors">{corridors}</div></section>
    <section class="card"><h2>Merchants</h2><div id="tenants">{tenants}</div></section>
  </div>
  <section class="card"><h2>Recent transactions</h2>
    <div class="scroll" id="feed">{feed}</div></section>
</main>
<footer>Simulated rails are labeled SIMULATED everywhere they appear. Prava is a real
sandbox integration and is labeled REAL. Auth rate and uplift cover card traffic only.</footer>
<script>
const F = ["stats", "authchart", "mixchart", "tiles", "corridors", "tenants", "feed"];
async function poll() {{
  await Promise.all(F.map(async n => {{
    try {{ document.getElementById(n).innerHTML = await (await fetch("/fragments/" + n)).text(); }}
    catch (e) {{ /* a dropped poll is not worth breaking the page over */ }}
  }}));
}}
setInterval(poll, 2000);
</script>
</body></html>"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


# --- fragments ---------------------------------------------------------------


def render_stats(summary: dict[str, Any]) -> str:
    """Hero numbers. The uplift tile carries its own uncertainty: at low volume
    it says so rather than showing a confident-looking number that is noise."""
    uplift = summary["uplift"]
    if uplift["pts"] is None:
        uplift_value, uplift_note = "&mdash;", "waiting for both arms"
    else:
        low, high = uplift["ci95_pts"]
        uplift_value = f"{uplift['pts']:+.1f} pts"
        uplift_note = (
            f"95% CI {low:+.1f} to {high:+.1f}"
            if uplift["significant"]
            else f"collecting &mdash; CI {low:+.1f} to {high:+.1f}"
        )

    router = summary["by_mode"]["router"]
    baseline = summary["by_mode"]["baseline"]
    recovered = summary.get("recovered_cents", 0)
    tiles = [
        ("Auth rate", _pct(summary["auth_rate"]), f"{summary['volume']} card payments"),
        ("Router vs baseline", uplift_value, uplift_note),
        ("Throughput", f"{summary.get('tx_per_min', 0):.0f}/min", "live traffic"),
        ("Router arm", _pct(router["auth_rate"]), f"{router['volume']} payments"),
        ("Baseline arm", _pct(baseline["auth_rate"]), f"{baseline['volume']} payments"),
        ("Revenue recovered", f"${recovered / 100:,.0f}", "vs the baseline's rate"),
    ]
    cells = "".join(
        f'<div class="stat"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{value}</div><div class="note">{note}</div></div>'
        for label, value, note in tiles
    )
    return f'<div class="stats">{cells}</div>'


def render_auth_chart(
    series: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    bucket_seconds: int = 30,
    width: int = 1100,
    height: int = 260,
) -> str:
    """Two lines on one axis, with injections marked where they happened.

    One y-scale by construction: both series are the same measure, so there is
    no second axis to invent a correlation with. Points carry a <title>, which
    gives a hover readout with no JavaScript at all.
    """
    left, right, top, bottom = 46, 16, 14, 26
    plot_w, plot_h = width - left - right, height - top - bottom

    buckets = sorted({int(row["bucket_ts"]) for row in series})
    if len(buckets) < 2:
        return (
            '<p class="empty">Collecting traffic &mdash; the chart needs at least two '
            f"{bucket_seconds}-second buckets before a line means anything.</p>"
        )

    by_mode: dict[str, dict[int, tuple[int, int]]] = {"router": {}, "baseline": {}}
    for row in series:
        mode = str(row["routing_mode"])
        if mode in by_mode:
            by_mode[mode][int(row["bucket_ts"])] = (
                int(row["succeeded"] or 0),
                int(row["volume"]),
            )

    t0, t1 = buckets[0], buckets[-1]
    span = max(1, t1 - t0)

    def x_of(ts: int) -> float:
        return left + (ts - t0) / span * plot_w

    def y_of(rate: float) -> float:
        return top + (1.0 - rate) * plot_h

    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="Authorization rate over time, router versus baseline" '
            f'style="display:block;max-width:100%">'
        )
    ]
    # Recessive solid hairline grid — never dashed.
    for tick in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = y_of(tick)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="var(--line)" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="var(--ink-3)">{int(tick * 100)}%</text>'
        )

    # Injection annotations, behind the data.
    for event in events:
        ts = int(event["epoch"])
        if not t0 <= ts <= t1:
            continue
        x = x_of(ts)
        colour = "var(--critical)" if "start" in str(event["kind"]) else "var(--good)"
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" '
            f'stroke="{colour}" stroke-width="1" stroke-opacity="0.55"/>'
            f"<title>{_esc(event['provider'])} {_esc(event['kind'])}</title>"
        )

    for mode, colour in (("router", "var(--router)"), ("baseline", "var(--baseline)")):
        points = []
        for ts in buckets:
            entry = by_mode[mode].get(ts)
            if entry is None or entry[1] == 0:
                continue
            rate = entry[0] / entry[1]
            points.append((x_of(ts), y_of(rate), rate, entry[1]))
        if not points:
            continue
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y, _, _) in enumerate(points)
        )
        parts.append(
            f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for x, y, rate, n in points:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" '
                f'stroke="var(--surface-2)" stroke-width="2">'
                f"<title>{mode}: {_pct(rate)} of {n}</title></circle>"
            )
        # Direct-label the endpoint only — a number on every point is chaos.
        end_x, end_y, end_rate, _ = points[-1]
        anchor = "end" if end_x > width - right - 60 else "start"
        dx = -8 if anchor == "end" else 8
        parts.append(
            f'<text x="{end_x + dx:.1f}" y="{end_y - 9:.1f}" text-anchor="{anchor}" '
            f'font-size="12" font-weight="600" fill="{colour}">{_pct(end_rate)}</text>'
        )

    minutes = round(span / 60)
    parts.append(
        f'<text x="{left}" y="{height - 7}" font-size="11" fill="var(--ink-3)">'
        f"{minutes} min ago</text>"
        f'<text x="{width - right}" y="{height - 7}" text-anchor="end" font-size="11" '
        f'fill="var(--ink-3)">now</text>'
    )
    parts.append("</svg>")

    legend = (
        '<div class="legend">'
        '<span><i class="swatch" style="background:var(--router)"></i>Router (learning)</span>'
        '<span><i class="swatch" style="background:var(--baseline)"></i>Baseline (round-robin)</span>'
        '<span><i class="swatch" style="background:var(--critical)"></i>Injection</span>'
        "</div>"
    )
    return legend + "".join(parts)


def render_tiles(providers: list[dict[str, Any]]) -> str:
    if not providers:
        return '<p class="empty">No providers configured.</p>'
    cells = []
    for tile in providers:
        badge = (
            '<span class="badge real">REAL · SANDBOX</span>'
            if tile["real"]
            else '<span class="badge sim">SIMULATED</span>'
        )
        state = _esc(tile["state"])
        meta = (
            f"{_pct(tile['auth_rate'])} auth · p95 {tile['p95_ms']}ms · {tile['attempts']} attempts"
            if tile["attempts"]
            else "no traffic yet"
        )
        cells.append(
            f'<div class="tile"><div class="name">{_esc(tile["display_name"])}{badge}</div>'
            f'<div class="meta"><span class="state s-{state}"><i class="dot"></i>'
            f"{state}</span></div>"
            f'<div class="meta">{meta}</div>'
            f'<div class="meta">fee {_esc(tile["fee"])}</div></div>'
        )
    return f'<div class="tiles">{"".join(cells)}</div>'


def _chain(attempts: list[dict[str, Any]]) -> str:
    """`stripe ✗ do_not_honor → adyen ✓` — the cascade, readable at a glance."""
    steps = []
    for attempt in attempts:
        name = _esc(str(attempt["provider"]).removesuffix("_sim"))
        if attempt["status"] == "succeeded":
            steps.append(f'{name} <span class="ok">&check;</span>')
        elif attempt["status"] == "pending":
            steps.append(f"{name} &hellip;")
        else:
            code = _esc(attempt["decline_code"] or "declined")
            steps.append(f'{name} <span class="no">&#10007;</span> {code}')
    return " &rarr; ".join(steps) or "&mdash;"


def render_feed(payments: list[dict[str, Any]]) -> str:
    if not payments:
        return '<p class="empty">No payments yet.</p>'
    rows = []
    for payment in payments:
        real = payment["method"] == "prava"
        badge = '<span class="badge real">REAL</span>' if real else ""
        status = _esc(payment["status"])
        css = "ok" if status == "succeeded" else "no" if status == "failed" else ""
        rows.append(
            f"<tr><td>{_esc(payment['created_ts'][11:19])}</td>"
            f"<td>${payment['amount_cents'] / 100:,.2f} {_esc(payment['currency'])}{badge}</td>"
            f"<td>{_esc(payment['segment'])}</td>"
            f"<td>{_esc(payment['routing_mode'])}</td>"
            f'<td class="{css}">{status}</td>'
            f'<td class="chain">{_chain(payment.get("attempts", []))}</td></tr>'
        )
    return (
        "<table><thead><tr><th>Time</th><th>Amount</th><th>Corridor</th>"
        "<th>Mode</th><th>Status</th><th>Route</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_page(**fragments: str) -> str:
    return _PAGE.format(style=STYLE, **fragments)


# --- panel 3: provider mix ---------------------------------------------------

RAIL_COLOURS = ("var(--rail-1)", "var(--rail-2)", "var(--rail-3)")


def render_mix_chart(
    series: list[dict[str, Any]],
    *,
    bucket_seconds: int = 30,
    width: int = 1100,
    height: int = 210,
) -> str:
    """A 100% stacked area of where the router's first attempts went.

    Part-to-whole over time, so the bands sum to the full height and a shift
    reads as one band eating another — which is exactly the story when a rail
    degrades and traffic slides elsewhere.

    Every band carries a visible label. That is not decoration: the dataviz
    validator flags the third rail hue at 2.74:1 on the light surface, below the
    3:1 bar, and the relief rule for that is direct labels or a table view. It
    also covers the weak tritan separation between the second and third hues in
    dark mode. Colour never carries identity here on its own.
    """
    left, right, top, bottom = 8, 8, 8, 22
    plot_w, plot_h = width - left - right, height - top - bottom

    buckets = sorted({int(row["bucket_ts"]) for row in series})
    if len(buckets) < 2:
        return (
            '<p class="empty">Collecting traffic &mdash; the mix needs at least two '
            f"{bucket_seconds}-second buckets.</p>"
        )

    rails = sorted({str(row["provider"]) for row in series})
    counts: dict[int, dict[str, int]] = {b: dict.fromkeys(rails, 0) for b in buckets}
    for row in series:
        counts[int(row["bucket_ts"])][str(row["provider"])] = int(row["attempts"])

    t0, t1 = buckets[0], buckets[-1]
    span = max(1, t1 - t0)

    def x_of(ts: int) -> float:
        return left + (ts - t0) / span * plot_w

    shares: dict[str, list[float]] = {rail: [] for rail in rails}
    for bucket in buckets:
        total = sum(counts[bucket].values()) or 1
        for rail in rails:
            shares[rail].append(counts[bucket][rail] / total)

    parts = [
        (
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="Share of router first attempts by provider over time" '
            f'style="display:block;max-width:100%">'
        )
    ]
    baseline = [0.0] * len(buckets)
    totals = {rail: sum(shares[rail]) / len(buckets) for rail in rails}
    for index, rail in enumerate(rails):
        upper = [baseline[i] + shares[rail][i] for i in range(len(buckets))]
        forward = " ".join(
            f"{'M' if i == 0 else 'L'}{x_of(b):.1f},{top + (1 - upper[i]) * plot_h:.1f}"
            for i, b in enumerate(buckets)
        )
        back = " ".join(
            f"L{x_of(b):.1f},{top + (1 - baseline[i]) * plot_h:.1f}"
            for i, b in reversed(list(enumerate(buckets)))
        )
        colour = RAIL_COLOURS[index % len(RAIL_COLOURS)]
        # The 2px surface stroke IS the gap between stacked segments — a border
        # drawn to separate marks would be the anti-pattern; this is a spacer.
        parts.append(
            f'<path d="{forward} {back} Z" fill="{colour}" fill-opacity="0.85" '
            f'stroke="var(--surface-2)" stroke-width="2"><title>{_esc(rail)}: '
            f"{totals[rail]:.0%} of first attempts</title></path>"
        )
        baseline = upper

    # Direct labels, one per band, at its widest point.
    baseline = [0.0] * len(buckets)
    for index, rail in enumerate(rails):
        upper = [baseline[i] + shares[rail][i] for i in range(len(buckets))]
        widest = max(range(len(buckets)), key=lambda i: shares[rail][i])
        if shares[rail][widest] > 0.10:  # too thin to hold a label
            mid_y = top + (1 - (baseline[widest] + upper[widest]) / 2) * plot_h
            anchor_x = min(max(x_of(buckets[widest]), left + 46), width - right - 46)
            # The label is placed at the band's widest point purely so it fits,
            # but it states the band's share across the WHOLE window — the same
            # number as its tooltip. Labelling the peak bucket instead read as
            # "adyen has 80%" when the band averaged 46%, which is two different
            # answers to one question.
            parts.append(
                f'<text x="{anchor_x:.1f}" y="{mid_y + 4:.1f}" text-anchor="middle" '
                f'font-size="11" font-weight="600" fill="#ffffff" '
                f'style="paint-order:stroke;stroke:rgba(0,0,0,.35);stroke-width:3px">'
                f"{_esc(rail.removesuffix('_sim'))} {totals[rail]:.0%}</text>"
            )
        baseline = upper

    minutes = round(span / 60)
    parts.append(
        f'<text x="{left}" y="{height - 6}" font-size="11" fill="var(--ink-3)">'
        f"{minutes} min ago</text>"
        f'<text x="{width - right}" y="{height - 6}" text-anchor="end" font-size="11" '
        f'fill="var(--ink-3)">now</text></svg>'
    )

    legend = (
        '<div class="legend">'
        + "".join(
            f'<span><i class="swatch" style="height:10px;border-radius:2px;'
            f'background:{RAIL_COLOURS[i % len(RAIL_COLOURS)]}"></i>'
            f"{_esc(rail.removesuffix('_sim'))}</span>"
            for i, rail in enumerate(rails)
        )
        + "</div>"
    )
    return legend + "".join(parts)


# --- panel 5: corridors ------------------------------------------------------


def render_corridors(rows: list[dict[str, Any]]) -> str:
    """Auth rate per corridor, router against baseline.

    The per-corridor split is the argument for segmenting the bandit at all: one
    global average would hide that a rail strong in US:USD:visa is weak in
    DE:EUR:visa, which is precisely the money the router recovers.
    """
    if not rows:
        return '<p class="empty">No corridor traffic yet.</p>'
    cells = []
    for row in rows:
        router = row.get("router_auth_rate")
        baseline = row.get("baseline_auth_rate")
        if router is None or baseline is None:
            delta = '<span class="empty">&mdash;</span>'
        else:
            points = (router - baseline) * 100
            css = "ok" if points > 0 else "no" if points < 0 else ""
            delta = f'<span class="{css}">{points:+.1f}</span>'
        cells.append(
            f"<tr><td>{_esc(row['segment'])}</td>"
            f'<td class="num">{row["volume"]}</td>'
            f'<td class="num">{_pct(router) if router is not None else "&mdash;"}</td>'
            f'<td class="num">{_pct(baseline) if baseline is not None else "&mdash;"}</td>'
            f'<td class="num">{delta}</td></tr>'
        )
    return (
        '<table><thead><tr><th>Corridor</th><th class="num">Vol</th>'
        '<th class="num">Router</th><th class="num">Baseline</th>'
        '<th class="num">Δ pts</th></tr></thead>'
        f"<tbody>{''.join(cells)}</tbody></table>"
    )


# --- panel 7: merchants ------------------------------------------------------


def render_tenants(rows: list[dict[str, Any]]) -> str:
    """Volume metered per merchant. Names only — this is a public page, and an
    email address is not an aggregate."""
    if not rows:
        return '<p class="empty">No merchant traffic yet.</p>'
    busiest = max(int(r["volume"]) for r in rows) or 1
    cells = []
    for row in rows:
        volume = int(row["volume"])
        authorized = int(row["authorized_cents"] or 0)
        width = max(6, round(volume / busiest * 90))
        fees = int(row.get("fees_cents") or 0)
        cells.append(
            f'<div class="merchant"><div class="who">{_esc(row["name"])}</div>'
            f'<div class="num">{volume} payments · ${authorized / 100:,.0f} authorized</div>'
            f'<div class="num">${fees / 100:,.2f} in fees</div>'
            f'<div><i class="bar" style="width:{width}px"></i></div></div>'
        )
    return f'<div class="strip">{"".join(cells)}</div>'
