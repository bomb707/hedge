"""Turning a snapshot into HTML. Formatting only — no arithmetic that means anything.

The one licence this module has is scale: a fixed-point integer of 1_230_000 may be shown as
"1.23", because that is the same number written for a human. It has no licence to compute a PnL,
an inventory, a target, or a permission. Those arrived already decided, and a dashboard that
recalculated them would be a second implementation of the accounting living in the place people
look when they want to know what is true.

Absence renders as "—", never as zero. An operator seeing a blank inventory and an operator
seeing 0 have been told different things, and only one of them is safe to act on.
"""

from __future__ import annotations

import html
from typing import Any, Final

__all__ = ["render_dashboard", "render_history"]

SHARE_SCALE: Final[int] = 1_000_000
MONEY_SCALE: Final[int] = 1_000_000
PRICE_SCALE: Final[int] = 1_000_000

STYLE: Final[str] = """
:root { color-scheme: dark; }
body { background:#101215; color:#dfe3e8; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
       margin:0; padding:16px 20px 40px; }
h1 { font-size:16px; margin:0 0 4px; letter-spacing:.04em; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.12em; color:#7d8794;
     margin:22px 0 6px; border-bottom:1px solid #23272d; padding-bottom:4px; }
table { border-collapse:collapse; width:100%; margin-bottom:4px; }
td, th { padding:3px 10px 3px 0; text-align:left; vertical-align:top; }
th { color:#7d8794; font-weight:400; width:220px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:0 28px; }
.na { color:#5a626c; }
.ok { color:#59c37a; } .warn { color:#e0b341; } .bad { color:#e06c5c; }
.pill { display:inline-block; padding:1px 7px; border-radius:9px; font-size:11px;
        letter-spacing:.06em; border:1px solid currentColor; }
.banner { padding:8px 12px; border-radius:4px; margin-bottom:14px; border:1px solid; }
.disc { background:#2a1618; border-color:#e06c5c; color:#e06c5c; }
.note { color:#7d8794; font-size:12px; }
form { display:inline; }
button { font:inherit; background:#1b1f24; color:#dfe3e8; border:1px solid #333a42;
         border-radius:3px; padding:5px 12px; cursor:pointer; }
button:hover { border-color:#59708c; }
.halt { border-color:#7a3b34; } .release { border-color:#3b5a3f; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _na(text: str = "—") -> str:
    return f'<span class="na">{text}</span>'


def _scaled(value: object, scale: int, places: int = 2) -> str:
    """A fixed-point integer, written for a person. `None` stays absent."""
    if value is None:
        return _na()
    if not isinstance(value, int):
        return _esc(value)
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), scale)
    return f"{sign}{whole}.{str(fraction).rjust(len(str(scale)) - 1, '0')[:places]}"


def money(value: object) -> str:
    return _scaled(value, MONEY_SCALE)


def shares(value: object) -> str:
    return _scaled(value, SHARE_SCALE)


def price(value: object) -> str:
    return _scaled(value, PRICE_SCALE, places=4)


def _status_class(status: object) -> str:
    text = str(status)
    if text in {"SAFE", "HEALTHY", "COMPLETE", "RESOLVED"}:
        return "ok"
    if text in {"RECOVERING", "INCOMPLETE", "UNRESOLVED", "UNKNOWN"}:
        return "warn"
    return "bad"


def _rows(pairs: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)
    return f"<table>{body}</table>"


def render_dashboard(
    snapshot: dict[str, Any] | None, age_seconds: float | None, message: str = ""
) -> str:
    """The live view. A missing snapshot is DISCONNECTED, never a flat market."""
    if snapshot is None:
        return _page(
            "operator — disconnected",
            f'<div class="banner disc"><b>NO SNAPSHOT</b> — the bot is not publishing, or has '
            f"not started a market yet. This is not an empty market; it is no data.</div>"
            f"{_message(message)}",
        )

    stale = age_seconds is not None and age_seconds > 5.0
    banner = ""
    if stale:
        banner = (
            f'<div class="banner disc"><b>STALE</b> — last snapshot '
            f"{age_seconds:.1f}s ago. The values below are the last known ones, not current "
            f"ones, and nothing here should be read as "
            f"&ldquo;the market is quiet&rdquo;.</div>"
        )

    up, down = snapshot.get("up", {}), snapshot.get("down", {})
    live = _rows(
        [
            ("market", _esc(snapshot.get("slug"))),
            ("condition", _esc(snapshot.get("condition_id") or "—")),
            ("phase", f'<span class="pill">{_esc(snapshot.get("phase"))}</span>'),
            ("ingress ordinal", _esc(snapshot.get("ingress_ordinal"))),
            ("elapsed", _seconds(snapshot.get("elapsed_seconds"))),
            ("remaining", _seconds(snapshot.get("remaining_seconds"))),
            ("snapshot age", "—" if age_seconds is None else f"{age_seconds:.1f}s"),
        ]
    )

    risk_state = snapshot.get("risk_state")
    risk = _rows(
        [
            (
                "risk state",
                f'<span class="{_status_class(risk_state)}">{_esc(risk_state or "—")}</span>',
            ),
            ("risk sequence", _esc(snapshot.get("risk_sequence"))),
            ("allows place", _flag(snapshot.get("allows_place"))),
            ("allows cancel", _flag(snapshot.get("allows_cancel"))),
            ("active", _list(snapshot.get("risk_active"))),
            ("latched", _list(snapshot.get("risk_latched"))),
            (
                "CLOB",
                f'<span class="{_status_class(snapshot.get("clob_status"))}">'
                f"{_esc(snapshot.get('clob_status'))}</span>",
            ),
            ("awaiting snapshot", _flag(snapshot.get("clob_awaiting_snapshot"))),
            (
                "BTC spot",
                f'<span class="{_status_class(snapshot.get("spot_status"))}">'
                f"{_esc(snapshot.get('spot_status'))}</span>",
            ),
            ("order stream", _esc(snapshot.get("order_stream_status") or "—")),
            ("control channel", _channel(snapshot.get("control_channel_available"))),
        ]
    )

    accounting = _rows(
        [
            (
                "n_up / n_down",
                f"{shares(snapshot.get('n_up'))} / {shares(snapshot.get('n_down'))}",
            ),
            ("inventory I", shares(snapshot.get("inventory"))),
            (
                "cost up / down",
                f"{money(snapshot.get('cost_up'))} / {money(snapshot.get('cost_down'))}",
            ),
            ("total cost", money(snapshot.get("total_cost"))),
            ("fees", money(snapshot.get("fees"))),
            ("rebate estimated", money(snapshot.get("estimated_rebates"))),
            ("rebate realised", money(snapshot.get("realised_rebates"))),
            ("PnL if UP (no rebate)", money(snapshot.get("pnl_if_up_without_rebate"))),
            ("PnL if DOWN (no rebate)", money(snapshot.get("pnl_if_down_without_rebate"))),
            ("PnL if UP (est. rebate)", money(snapshot.get("pnl_if_up_estimated_rebate"))),
            ("PnL if DOWN (est. rebate)", money(snapshot.get("pnl_if_down_estimated_rebate"))),
            ("favourite", _esc(snapshot.get("favourite") or "—")),
            ("target inventory", shares(snapshot.get("target_inventory"))),
        ]
    )

    centre = _rows(
        [
            ("raw centre", _ratio(snapshot)),
            ("quantized centre", price(snapshot.get("quantized_centre"))),
            ("source", _esc(snapshot.get("centre_source"))),
            ("status", f'<span class="pill">{_esc(snapshot.get("centre_status"))}</span>'),
        ]
    )

    settlement = _rows(
        [
            ("resolution", _esc(snapshot.get("resolution_state") or "—")),
            ("winner", _esc(snapshot.get("winning_outcome") or "—")),
            ("authoritative block", _esc(snapshot.get("authoritative_block") or "—")),
            ("payout", _list(snapshot.get("payout_numerators"))),
            ("redemption", '<span class="pill bad">DISABLED</span>'),
        ]
    )

    telemetry = _rows(
        [
            ("decisions persisted", _esc(snapshot.get("decisions_persisted"))),
            ("risk records persisted", _esc(snapshot.get("risk_records_persisted"))),
            ("dropped", _esc(snapshot.get("dropped_records"))),
            ("sink errors", _esc(snapshot.get("sink_errors"))),
            ("verification", _esc(snapshot.get("verification_status") or "—")),
            ("control audit", _flag(snapshot.get("control_audit_complete"))),
            (
                "telemetry complete",
                _na("decided at close")
                if snapshot.get("telemetry_complete") is None
                else _flag(snapshot.get("telemetry_complete")),
            ),
        ]
    )

    latency = _rows(
        [
            ("decide (P8 decide_duration)", _ns(snapshot.get("decide_ns"))),
            ("receive → decide", _ns(snapshot.get("receive_to_decide_ns"))),
            ("prepare", _ns(snapshot.get("prepare_ns"))),
            ("reconcile", _ns(snapshot.get("reconcile_ns"))),
            ("receive → reconcile", _ns(snapshot.get("receive_to_reconcile_ns"))),
            ("sampled at ordinal", _esc(snapshot.get("latency_sample_ordinal") or "—")),
        ]
    )
    points = snapshot.get("observation_points") or {}
    coherence = (
        '<p class="note">observation points — '
        + " &middot; ".join(
            f"{_esc(k)}: {_esc(v if v is not None else 'latest')}" for k, v in points.items()
        )
        + ". P8 samples, so a latency figure may come from an earlier cycle than the decision "
        "beside it; the ordinal above says which.</p>"
    )

    return _page(
        f"operator — {snapshot.get('slug')}",
        banner
        + _message(message)
        + _safety(snapshot)
        + _controls()
        + '<div class="grid">'
        + f"<div><h2>market</h2>{live}<h2>risk &amp; health</h2>{risk}"
        + f"<h2>centre</h2>{centre}</div>"
        + f"<div><h2>accounting</h2>{accounting}</div></div>"
        + f"<h2>execution</h2>{_sides(up, down)}"
        + f'<div class="grid"><div><h2>settlement</h2>{settlement}</div>'
        + f"<div><h2>telemetry</h2>{telemetry}</div></div>"
        + f"<h2>latency (P8 measurements)</h2>{latency}{coherence}"
        + f"<h2>strategy parameters</h2>{_parameters(snapshot.get('parameters') or [])}"
        + f"<h2>operator commands</h2>{_commands(snapshot.get('accepted_commands') or [])}",
    )


def _safety(snapshot: dict[str, Any]) -> str:
    live = snapshot.get("live_trading_enabled")
    redeem = snapshot.get("redemption_enabled")
    live_class = "bad" if live else "ok"
    return (
        f'<p class="note">LIVE TRADING: <span class="pill {live_class}">'
        f"{'ENABLED' if live else 'DISABLED'}</span> &nbsp; REDEMPTION: "
        f'<span class="pill {"bad" if redeem else "ok"}">'
        f"{'ENABLED' if redeem else 'DISABLED'}</span> &nbsp; "
        "neither can be changed from here; P14 owns live capital.</p>"
    )


def _controls() -> str:
    """POST only, and each button is one explicit action.

    A GET must never change anything: a browser prefetch, a bookmark or a refresh would
    otherwise halt or release a live market by accident. The command id is minted per render, so
    re-posting the same form is the same command rather than a second one.
    """
    return (
        '<h2>control</h2><p class="note">These enqueue an ordered command. They do not change '
        "trading state directly, and a release clears the operator halt only — every other risk "
        "condition still applies.</p>"
        '<form method="post" action="/control"><input type="hidden" name="kind" '
        'value="OPERATOR_HALT"><button class="halt" type="submit">OPERATOR HALT</button></form> '
        '<form method="post" action="/control"><input type="hidden" name="kind" '
        'value="RELEASE_OPERATOR_HALT"><button class="release" type="submit">'
        "RELEASE OPERATOR HALT</button></form>"
    )


def _sides(up: dict[str, Any], down: dict[str, Any]) -> str:
    header = (
        "<tr><th></th><th>strategy wanted</th><th>execution allowed</th><th>action</th>"
        "<th>reason</th><th>resting</th><th>queue ahead</th><th>post-only</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<th>{_esc(side.get('outcome'))}</th>"
        f"<td>{_intent(side.get('strategy_price'), side.get('strategy_size'))}</td>"
        f"<td>{_intent(side.get('executable_price'), side.get('executable_size'))}</td>"
        f"<td>{_esc(side.get('action') or '—')}</td>"
        f"<td>{_esc(side.get('reason') or '—')}</td>"
        f"<td>{_resting(side)}</td>"
        f"<td>{shares(side.get('queue_ahead'))} "
        f"<span class='na'>{_esc(side.get('queue_confidence') or '')}</span></td>"
        f"<td>{_esc(side.get('preparation_outcome') or '—')}</td>"
        "</tr>"
        for side in (up, down)
        if side
    )
    return (
        f"<table>{header}{rows}</table>"
        '<p class="note">"strategy wanted" is what the economics asked for; "execution allowed" '
        "is what survived the risk verdict. They differ exactly when safety withdrew a quote.</p>"
    )


def _intent(price_value: object, size_value: object) -> str:
    if price_value is None and size_value is None:
        return _na()
    return f"{price(price_value)} &times; {shares(size_value)}"


def _resting(side: dict[str, Any]) -> str:
    if not side.get("live_client_order_id"):
        return _na()
    return (
        f"{price(side.get('live_price'))} &times; {shares(side.get('live_remaining_size'))}"
        f" <span class='na'>{_esc(side.get('live_status') or '')}</span>"
    )


def _parameters(parameters: list[dict[str, Any]]) -> str:
    header = "<tr><th>parameter</th><th>value</th><th>status</th><th>open item</th><th></th></tr>"
    rows = "".join(
        "<tr>"
        f"<th>{_esc(p.get('name'))}</th><td>{_esc(p.get('value'))}</td>"
        f'<td><span class="pill {_label_class(p.get("status"))}">{_esc(p.get("status"))}'
        "</span></td>"
        f"<td>{_esc(p.get('open_item') or '—')}</td>"
        f'<td class="na">{_esc(p.get("note") or "")}</td></tr>'
        for p in parameters
    )
    return (
        f"<table>{header}{rows}</table>"
        '<p class="note">OPEN means the frozen sources do not establish this value and it has '
        "not been closed by evidence. FITTED means it was chosen to match observed behaviour. "
        "Neither is a confirmed strategy constant, and this view cannot change any of them.</p>"
    )


def _label_class(status: object) -> str:
    return {"CONFIRMED": "ok", "OPERATIONAL": "warn"}.get(str(status), "bad")


def _commands(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return '<p class="na">no operator command has been issued for this market.</p>'
    header = (
        "<tr><th>command</th><th>kind</th><th>accepted</th><th>ingress</th>"
        "<th>risk seq</th><th>state</th><th>detail</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<th>{_esc(e.get('command_id'))}</th><td>{_esc(e.get('kind'))}</td>"
        f"<td>{_flag(e.get('accepted'))}</td><td>{_esc(e.get('ingress_ordinal'))}</td>"
        f"<td>{_esc(e.get('risk_sequence'))}</td><td>{_esc(e.get('risk_state'))}</td>"
        f'<td class="na">{_esc(e.get("detail") or "")}</td></tr>'
        for e in entries
    )
    return f"<table>{header}{rows}</table>"


def _ns(value: object) -> str:
    """Nanoseconds, formatted. Absent means unsampled, and says so rather than showing zero."""
    if not isinstance(value, int):
        return _na("not sampled")
    return f"{value:,} ns"


def _channel(value: object) -> str:
    if value is None:
        return _na("no bridge")
    if value:
        return '<span class="ok">available</span>'
    return '<span class="bad">CONTROL CHANNEL UNAVAILABLE</span>'


def _flag(value: object) -> str:
    if value is None:
        return _na()
    return '<span class="ok">yes</span>' if value else '<span class="bad">no</span>'


def _list(values: object) -> str:
    if not values or not isinstance(values, list | tuple):
        return _na("none")
    return _esc(", ".join(str(item) for item in values))


def _seconds(value: object) -> str:
    if not isinstance(value, int | float):
        return _na()
    return f"{float(value):.1f}s"


def _ratio(snapshot: dict[str, Any]) -> str:
    numerator = snapshot.get("raw_centre_numerator")
    denominator = snapshot.get("raw_centre_denominator")
    if numerator is None or denominator is None:
        return _na()
    return f"{numerator}/{denominator}"


def _message(message: str) -> str:
    if not message:
        return ""
    return f'<div class="banner disc">{_esc(message)}</div>'


def render_history(rows: list[dict[str, Any]], message: str = "") -> str:
    """Persisted markets, each labelled by what its telemetry can actually support."""
    if not rows:
        body = '<p class="na">no persisted markets found.</p>'
    else:
        header = (
            "<tr><th>market</th><th>verification</th><th>decisions</th><th>risk rows</th>"
            "<th>PLACE by risk state</th><th>evidence</th></tr>"
        )
        cells = "".join(
            "<tr>"
            f"<th>{_esc(r.get('slug') or r.get('source'))}</th>"
            f'<td><span class="{_status_class(r.get("verification_status"))}">'
            f"{_esc(r.get('verification_status'))}</span></td>"
            f"<td>{_esc(r.get('decisions'))}</td><td>{_esc(r.get('risk_records'))}</td>"
            f"<td>{_esc(r.get('places_by_risk_state'))}</td>"
            f"<td>{_eligibility(r)}</td></tr>"
            for r in rows
        )
        body = f"<table>{header}{cells}</table>"
    return _page(
        "operator — history",
        _message(message)
        + "<h2>persisted markets</h2>"
        + body
        + '<p class="note">An INCOMPLETE market is shown rather than hidden — it is still the '
        "record of what happened — but it is not eligible as empirical evidence, because there "
        "is no way to tell from the surviving rows which ones are missing.</p>",
    )


def _eligibility(row: dict[str, Any]) -> str:
    if row.get("evidence_eligible"):
        return '<span class="ok">eligible</span>'
    return '<span class="bad">NOT ELIGIBLE FOR EMPIRICAL STRATEGY EVIDENCE</span>'


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{STYLE}</style>"
        "<meta http-equiv='refresh' content='2'></head><body>"
        f"<h1>{_esc(title)}</h1>"
        "<p class='note'><a href='/'>live</a> &middot; <a href='/history'>history</a></p>"
        f"{body}</body></html>"
    )
