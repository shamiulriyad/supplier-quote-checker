"""Plotly charts. Colors: validated status trio (labels + legend always shown, 2px gaps between fills)."""
import plotly.graph_objects as go

from .theme import C, CHART, DECISION_LABEL

ORDER = ["APPROVE", "NEEDS_REVIEW", "REJECT"]


def _layout(fig: go.Figure, height: int = 260, legend: bool = False) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=24, t=8, b=8), paper_bgcolor=C["surface"], plot_bgcolor=C["surface"],
        font=dict(family="Inter, system-ui, sans-serif", size=12, color=C["text"]),
        showlegend=legend, barcornerradius=4, bargap=0.35,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(color=C["muted"])),
        hoverlabel=dict(bgcolor=C["surface"], bordercolor=C["border"], font=dict(color=C["text"])),
    )
    fig.update_xaxes(gridcolor="#EEF0F3", zeroline=False, linecolor=C["border"], tickfont=dict(color=C["muted"]))
    fig.update_yaxes(gridcolor="#EEF0F3", zeroline=False, linecolor=C["border"], tickfont=dict(color=C["muted"]))
    return fig


def decisions_chart(counts: dict[str, int]) -> go.Figure:
    """System decisions: one bar per decision, labelled on the axis and with its value."""
    keys = [k for k in ORDER if k in counts]
    fig = go.Figure(go.Bar(
        y=[DECISION_LABEL[k] for k in keys], x=[counts[k] for k in keys], orientation="h",
        marker=dict(color=[CHART[k] for k in keys], line=dict(color=C["surface"], width=2)),
        text=[counts[k] for k in keys], textposition="outside", textfont=dict(color=C["text"]), cliponaxis=False,
        hovertemplate="%{y}: %{x} documents<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(title=None, dtick=1 if max(counts.values(), default=0) < 12 else None)
    return _layout(fig, height=200)


def review_chart(waiting: int, approved: int, rejected: int) -> go.Figure:
    """Human review: waiting vs decided by a person."""
    labels = ["Waiting for a person", "Approved by a person", "Rejected by a person"]
    values = [waiting, approved, rejected]
    colors = [CHART["NEEDS_REVIEW"], CHART["APPROVE"], CHART["REJECT"]]
    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h", marker=dict(color=colors, line=dict(color=C["surface"], width=2)),
        text=values, textposition="outside", textfont=dict(color=C["text"]), cliponaxis=False,
        hovertemplate="%{y}: %{x}<extra></extra>"))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(dtick=1 if max(values) < 12 else None)
    return _layout(fig, height=200)


def history_chart(batches: list[dict]) -> go.Figure:
    """Documents per batch over time, stacked by system decision."""
    batches = list(reversed(batches))  # oldest first
    x = [b["created_at"][5:16].replace("T", " ") + f" ({b['batch_id'][-4:]})" for b in batches]
    fig = go.Figure()
    for key, col in (("APPROVE", "approved"), ("NEEDS_REVIEW", "needs_review"), ("REJECT", "rejected")):
        vals = [b.get(col) or 0 for b in batches]
        fig.add_bar(name=DECISION_LABEL[key], x=x, y=vals, marker=dict(color=CHART[key],
                    line=dict(color=C["surface"], width=2)),
                    text=[v or "" for v in vals], textposition="inside", insidetextfont=dict(color="#FFFFFF"),
                    hovertemplate="%{x}<br>" + DECISION_LABEL[key] + ": %{y}<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_xaxes(type="category", showgrid=False)
    fig.update_yaxes(title=dict(text="Documents", font=dict(color=C["muted"])))
    return _layout(fig, height=280, legend=True)


def rule_failures_chart(counts: list[dict]) -> go.Figure:
    """How often each rule failed. Single series, so no legend; rule names on the axis."""
    rows = [c for c in counts if c["fail"]]
    rows.sort(key=lambda c: c["fail"])
    fig = go.Figure(go.Bar(
        y=[f"{c['rule']} · {c['title']}" for c in rows], x=[c["fail"] for c in rows], orientation="h",
        marker=dict(color=CHART["accent"], line=dict(color=C["surface"], width=2)),
        text=[c["fail"] for c in rows], textposition="outside", textfont=dict(color=C["text"]), cliponaxis=False,
        hovertemplate="%{y}<br>Failed in %{x} documents<extra></extra>"))
    fig.update_yaxes(showgrid=False)
    fig.update_xaxes(dtick=1)
    return _layout(fig, height=max(180, 34 * len(rows) + 40))
