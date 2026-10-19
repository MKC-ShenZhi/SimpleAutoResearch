#!/usr/bin/env python3
"""Recompute the collaborator-report numbers, tables and multi-panel figure.

Inputs are the *saved* calibration results (no model is refitted, nothing new is
simulated):
    <materials>/recorded_results.json   monthly fit/predict timings, cohort bookkeeping
    <materials>/monthly_metrics.csv     per-month, per-model, per-policy metrics
    <materials>/overall_metrics.csv     pooled test metrics
    <materials>/periods.csv             train / validation / refit / test coverage
    <materials>/cohorts.csv             monthly fit size and test-hour eligibility

The script re-derives every summary quoted in the report, cross-checks the CSVs
against the JSON and against each other, and writes:

    report/derived_metrics.json, report/derived_metrics.md
    report/tables/*.csv, report/tables/tables.md
    report/figures/monthly_update_panel.{png,pdf}, report/figures/figure_data.csv

It exits non-zero on any inconsistency instead of publishing numbers.

Run:  python report/make_figure_tables_v2.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, NullFormatter

HERE = Path(__file__).resolve().parent
WS = HERE.parent

# Inputs travel with the report as frozen copies in report/inputs/, so the
# package can be moved anywhere.  An explicit CALIB_INPUTS environment
# variable and a workspace-level "materials" directory are accepted as
# alternatives; no absolute server path is baked in.
_CANDIDATES = []
if os.environ.get("CALIB_INPUTS"):
    _CANDIDATES.append(Path(os.environ["CALIB_INPUTS"]).expanduser())
_CANDIDATES += [HERE / "inputs", WS / "materials"]
MAT = next((p for p in _CANDIDATES if (p / "recorded_results.json").is_file()), None)
if MAT is None:
    sys.exit(
        "Could not find the frozen input files. Put recorded_results.json, "
        "monthly_metrics.csv, overall_metrics.csv, periods.csv and cohorts.csv "
        "in report/inputs/ (next to this script), or point CALIB_INPUTS at the "
        "directory that holds them."
    )
print(f"inputs: {MAT}")
FIG = HERE / "figures"
TAB = HERE / "tables"
FIG.mkdir(parents=True, exist_ok=True)
TAB.mkdir(parents=True, exist_ok=True)

LINEAR, QUAD = "linear", "quadratic"
FROZEN, UPDATED = "frozen", "updated"
MODELS = [LINEAR, QUAD]
POLICIES = [FROZEN, UPDATED]
C = {LINEAR: "#1b4f72", QUAD: "#b9770e"}
FAIL = []
WARN = []


def check(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        FAIL.append(msg)


def warn(msg):
    print(f"[WARN] {msg}")
    WARN.append(msg)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def find_overall(node, out=None):
    """Collect every dict that looks like one overall-metric record."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if "model" in node and "rmse" in node:
            out.append(node)
        for v in node.values():
            find_overall(v, out)
    elif isinstance(node, list):
        for v in node:
            find_overall(v, out)
    return out


def num(v):
    return float(v)


# ---------------------------------------------------------------- load inputs
rr = json.loads((MAT / "recorded_results.json").read_text(encoding="utf-8"))
monthly = read_csv(MAT / "monthly_metrics.csv")
overall = read_csv(MAT / "overall_metrics.csv")
periods = {r["period"]: r for r in read_csv(MAT / "periods.csv")}
cohorts_csv = read_csv(MAT / "cohorts.csv")

months = list(rr["months"])
check(months == [r["month"] for r in cohorts_csv],
      "JSON month list == cohorts.csv month order")
check(len(months) == 6 and months[0] == "2004-11" and months[-1] == "2005-04",
      "six monthly cohorts spanning 2004-11 .. 2005-04")

M = {(r["month"], r["model"], r["policy"]): r for r in monthly}
check(len(M) == len(months) * len(MODELS) * len(POLICIES),
      "monthly table has every month x model x policy row")
O = {(r["model"], r["policy"]): r for r in overall}
check(len(O) == 4, "pooled table has four model/policy rows")

n_month = {m: {k: int(float(M[(m, LINEAR, FROZEN)]["n"])) for k in MODELS} for m in months}
check(all(float(M[(m, mo, po)]["n"]) == float(M[(m, LINEAR, FROZEN)]["n"])
          for m in months for mo in MODELS for po in POLICIES),
      "same test-hour count for all four series within a month")

# pooled n must equal the sum of monthly test hours
n_tot = int(float(O[(LINEAR, FROZEN)]["n"]))
check(n_tot == sum(n_month[m][LINEAR] for m in months) ==
      int(float(periods["test"]["eligible"])),
      f"pooled n == sum(monthly n) == test eligible ({n_tot})")

# pooled metrics must be reproducible from the monthly rows (a real consistency
# test: it re-derives the pooled RMSE/MAE/bias from per-month values and n)
for mo in MODELS:
    for po in POLICIES:
        n = [float(M[(m, mo, po)]["n"]) for m in months]
        for field in ("rmse", "mae", "bias"):
            if field == "rmse":
                pooled = math.sqrt(sum(ni * float(M[(m, mo, po)]["rmse"]) ** 2
                                       for ni, m in zip(n, months)) / sum(n))
            else:
                pooled = sum(ni * float(M[(m, mo, po)][field])
                             for ni, m in zip(n, months)) / sum(n)
            stored = float(O[(mo, po)][field])
            check(abs(pooled - stored) < 5e-6,
                  f"pooled {mo}/{po} {field} reproducible from monthly rows "
                  f"({pooled:.6f} vs {stored:.6f})")

# JSON overall block (if identifiable) must agree with the CSV
json_recs = find_overall(rr)
if len(json_recs) == 4:
    for rec in json_recs:
        key = (rec["model"], rec["policy"])
        if key in O:
            check(abs(num(rec["rmse"]) - num(O[key]["rmse"])) < 1e-9,
                  f"JSON vs CSV pooled rmse {key[0]}/{key[1]}")
else:
    warn(f"JSON overall block not identified ({len(json_recs)} candidate records); "
         "CSV treated as the frozen pooled table")

# ------------------------------------------------------------- timing records
timings = rr["timings"]
check(isinstance(timings, list), "timings stored as a list of per-run records")
by_month = {mo: {} for mo in MODELS}
for i, entry in enumerate(timings):
    mo = entry.get("model")
    month = entry.get("month") or entry.get("cohort")
    if month is None and "fit" in entry:
        month = entry["fit"].get("month")
    by_month.setdefault(mo, {})
    if month is None:
        raise SystemExit(f"timing record {i} has no month label; cannot align")
    by_month[mo][month] = entry
check(all(set(by_month[mo]) == set(months) for mo in MODELS),
      "timing records cover each model x month exactly once")

TIM = {}
for mo in MODELS:
    for m in months:
        e = by_month[mo][m]
        fit, pred = e["fit"], e["prediction"]
        if "seconds_each" in fit:
            med = statistics.median(fit["seconds_each"])
            check(abs(med - fit["median_seconds"]) < 1e-12,
                  f"{mo} {m} fit median == median(seconds_each)")
        TIM[(mo, m)] = {
            "fit_ms": 1000.0 * float(fit["median_seconds"]),
            "fit_min_ms": 1000.0 * float(fit["min_seconds"]) if "min_seconds" in fit else None,
            "fit_max_ms": 1000.0 * float(fit["max_seconds"]) if "max_seconds" in fit else None,
            "pred_ms": 1000.0 * float(pred["median_seconds"]),
            "fit_us_per_row": fit.get("median_microseconds_per_row"),
            "pred_us_per_row": pred.get("median_microseconds_per_row"),
            "pred_rows_implied": (float(pred["median_seconds"]) /
                                  (float(pred["median_microseconds_per_row"]) * 1e-6))
            if pred.get("median_microseconds_per_row") else None,
        }

# the stored totals are sums of the monthly medians, not independent timings
TOT = {}
for mo in MODELS:
    for kind, col in (("fit", "fit_ms"), ("prediction", "pred_ms")):
        s = sum(TIM[(mo, m)][col] for m in months) / 1000.0
        stored = float(rr["total_median_cost_seconds"][mo][kind])
        TOT[(mo, kind)] = stored
        check(abs(s - stored) < 1e-12,
              f"stored total {mo}/{kind} == sum of six monthly medians")

AMORT = {mo: 1e6 * TOT[(mo, "fit")] / n_tot for mo in MODELS}
ANNUAL = {mo: 2.0 * TOT[(mo, "fit")] for mo in MODELS}

# ---------------------------------------------------------------- table 1/2
rel = {}
for mo in MODELS:
    f, u = O[(mo, FROZEN)], O[(mo, UPDATED)]
    rel[mo] = {
        "ratio": float(f["rmse"]) / float(u["rmse"]),
        "rel_rmse": 100.0 * (float(f["rmse"]) - float(u["rmse"])) / float(f["rmse"]),
        "rel_p95": 100.0 * (float(f["p95_abs_error"]) - float(u["p95_abs_error"]))
        / float(f["p95_abs_error"]),
        "rel_mae": 100.0 * (float(f["mae"]) - float(u["mae"])) / float(f["mae"]),
    }
form_gap = float(O[(QUAD, FROZEN)]["rmse"]) / float(O[(LINEAR, UPDATED)]["rmse"])

with open(TAB / "table1_pooled.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["model", "policy", "n", "rmse", "mae", "abs_bias", "p95_abs_error", "r2",
                "negative_predictions", "negative_pct"])
    for mo in MODELS:
        for po in POLICIES:
            r = O[(mo, po)]
            w.writerow([mo, po, r["n"], f"{float(r['rmse']):.4f}", f"{float(r['mae']):.4f}",
                        f"{abs(float(r['bias'])):.4f}", f"{float(r['p95_abs_error']):.4f}",
                        f"{float(r['r2']):.4f}", r["negative_prediction_count"],
                        f"{100 * float(r['negative_prediction_count']) / float(r['n']):.2f}"])

with open(TAB / "table2_monthly_delta.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["month", "n", "model", "rmse_frozen", "rmse_updated", "delta_rmse",
                "pct_rmse", "mae_frozen", "mae_updated", "delta_mae"])
    for m in months:
        for mo in MODELS:
            f, u = M[(m, mo, FROZEN)], M[(m, mo, UPDATED)]
            d = float(u["rmse"]) - float(f["rmse"])
            w.writerow([m, int(float(f["n"])), mo, f"{float(f['rmse']):.4f}",
                        f"{float(u['rmse']):.4f}", f"{d:+.4f}",
                        f"{100 * d / float(f['rmse']):+.1f}",
                        f"{float(f['mae']):.4f}", f"{float(u['mae']):.4f}",
                        f"{float(u['mae']) - float(f['mae']):+.4f}"])

# ---------------------------------------------------------------- table 3
cov = []
for m in months:
    c = next(r for r in cohorts_csv if r["month"] == m)
    avail, elig = int(c["available"]), int(c["eligible"])
    cov.append({
        "month": m, "fit_n": int(c["fit_n"]), "available": avail, "eligible": elig,
        "excluded": int(c["excluded"]), "coverage": 100.0 * elig / avail,
        "share_test": 100.0 * elig / n_tot,
    })
check(sum(r["excluded"] for r in cov) + n_tot == int(float(periods["test"]["available"])),
      "monthly eligible + excluded == test available hours")
check(sum(r["eligible"] for r in cov) == n_tot, "monthly eligible sums to pooled n")
check(sum(r["available"] for r in cov) == int(float(periods["test"]["available"])),
      "monthly available hours sum to test hours available")
with open(TAB / "table3_coverage.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["month", "fit_rows", "test_hours_available", "test_hours_eligible",
                "excluded", "coverage_pct", "share_of_test_pct"])
    for r in cov:
        w.writerow([r["month"], r["fit_n"], r["available"], r["eligible"], r["excluded"],
                    f"{r['coverage']:.1f}", f"{r['share_test']:.1f}"])

# ---------------------------------------------------------------- table 4
with open(TAB / "table4_timing.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["model", "month", "fit_median_ms", "fit_min_ms", "fit_max_ms",
                "predict_median_ms", "recorded_predict_us_per_row", "implied_batch_rows"])
    for mo in MODELS:
        for m in months:
            t = TIM[(mo, m)]
            f = (lambda v: "" if v is None else f"{v:.3f}")
            w.writerow([mo, m, f"{t['fit_ms']:.3f}", f(t["fit_min_ms"]), f(t["fit_max_ms"]),
                        f"{t['pred_ms']:.4f}",
                        "" if t["pred_us_per_row"] is None else f"{t['pred_us_per_row']:.3f}",
                        "" if t["pred_rows_implied"] is None else f"{t['pred_rows_implied']:.0f}"])
    for mo in MODELS:
        w.writerow([mo, "six-month total", f"{1000 * TOT[(mo, 'fit')]:.3f}", "", "",
                    f"{1000 * TOT[(mo, 'prediction')]:.4f}", "", ""])
        w.writerow([mo, "annualised (12 refits)", f"{1000 * ANNUAL[mo]:.3f}", "", "", "", "", ""])

# ---------------------------------------------------------------- markdown tables
def md_table(header, rows, align=None):
    align = align or ["---"] * len(header)
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


t1 = md_table(
    ["Form", "Policy", "n", "RMSE", "MAE", "|bias|", "P95 |err|", "R2", "Pred. < 0"],
    [[mo, po, O[(mo, po)]["n"], f"{float(O[(mo, po)]['rmse']):.4f}",
      f"{float(O[(mo, po)]['mae']):.4f}", f"{abs(float(O[(mo, po)]['bias'])):.4f}",
      f"{float(O[(mo, po)]['p95_abs_error']):.4f}", f"{float(O[(mo, po)]['r2']):.4f}",
      f"{int(float(O[(mo, po)]['negative_prediction_count']))} "
      f"({100 * float(O[(mo, po)]['negative_prediction_count']) / float(O[(mo, po)]['n']):.1f}%)"]
     for mo in MODELS for po in POLICIES],
    ["---", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:"])

t2 = md_table(
    ["Month", "n", "Form", "RMSE frozen", "RMSE updated", "ΔRMSE", "Δ%", "MAE frozen", "MAE updated"],
    [[m, int(float(M[(m, mo, FROZEN)]["n"])), mo,
      f"{float(M[(m, mo, FROZEN)]['rmse']):.3f}", f"{float(M[(m, mo, UPDATED)]['rmse']):.3f}",
      f"{float(M[(m, mo, UPDATED)]['rmse']) - float(M[(m, mo, FROZEN)]['rmse']):+.3f}",
      f"{100 * (float(M[(m, mo, UPDATED)]['rmse']) - float(M[(m, mo, FROZEN)]['rmse'])) / float(M[(m, mo, FROZEN)]['rmse']):+.1f}",
      f"{float(M[(m, mo, FROZEN)]['mae']):.3f}", f"{float(M[(m, mo, UPDATED)]['mae']):.3f}"]
     for m in months for mo in MODELS],
    ["---", "---:", "---", "---:", "---:", "---:", "---:", "---:", "---:"])

t3 = md_table(
    ["Month", "Fit rows", "Test hours avail.", "Eligible", "Excluded", "Coverage", "Share of test"],
    [[r["month"], f"{r['fit_n']:,}", r["available"], r["eligible"], r["excluded"],
      f"{r['coverage']:.1f}%", f"{r['share_test']:.1f}%"] for r in cov]
    + [["test total", f"{O[(LINEAR, FROZEN)]['n']}", periods["test"]["available"], n_tot,
        int(float(periods["test"]["available"])) - n_tot,
        f"{100 * n_tot / float(periods['test']['available']):.1f}%", "100.0%"]],
    ["---", "---:", "---:", "---:", "---:", "---:", "---:"])

t4 = md_table(
    ["Form", "Month", "Fit median (ms)", "Predict median (ms)", "Predict µs/row (recorded)"],
    [[mo, m, f"{TIM[(mo, m)]['fit_ms']:.2f}", f"{TIM[(mo, m)]['pred_ms']:.3f}",
      "" if TIM[(mo, m)]["pred_us_per_row"] is None else f"{TIM[(mo, m)]['pred_us_per_row']:.2f}"]
     for mo in MODELS for m in months]
    + [[mo, "six-month total", f"{1000 * TOT[(mo, 'fit')]:.1f}", f"{1000 * TOT[(mo, 'prediction')]:.2f}", "—"]
       for mo in MODELS]
    + [[mo, "annualised (12 refits)", f"{1000 * ANNUAL[mo]:.1f}", "—", "—"] for mo in MODELS],
    ["---", "---", "---:", "---:", "---:"])

(TAB / "tables.md").write_text(
    "# Tables generated from the saved calibration results\n\n"
    "Generated by `report/make_figure_tables_v2.py`; do not edit by hand.\n\n"
    f"## Table 1. Pooled test metrics (3,493 test hours, 2004-11-01 .. 2005-04-04)\n\n{t1}\n\n"
    f"## Table 2. Month-by-month effect of the monthly update\n\n{t2}\n\n"
    f"## Table 3. Sample coverage and expanding fit window\n\n{t3}\n\n"
    f"## Table 4. Cost of one monthly refit and of one prediction pass\n\n{t4}\n",
    encoding="utf-8")

# ---------------------------------------------------------------- figure data
rows = []
for m in months:
    for mo in MODELS:
        for po in POLICIES:
            r = M[(m, mo, po)]
            rows.append(["b", f"{mo}/{po}", m, f"{float(r['rmse']):.6f}", int(float(r["n"]))])
for mo in MODELS:
    rows.append(["a", f"{mo}/{FROZEN}", "pooled", f"{float(O[(mo, FROZEN)]['rmse']):.6f}", n_tot])
    rows.append(["a", f"{mo}/{UPDATED}", "pooled", f"{float(O[(mo, UPDATED)]['rmse']):.6f}", n_tot])
    for m in months:
        d = 100 * (float(M[(m, mo, UPDATED)]["rmse"]) - float(M[(m, mo, FROZEN)]["rmse"])) / float(M[(m, mo, FROZEN)]["rmse"])
        rows.append(["c", mo, m, f"{d:+.4f}", int(float(M[(m, mo, FROZEN)]["n"]))])
for r in cov:
    rows.append(["d", "fit_rows", r["month"], r["fit_n"], r["available"]])
    rows.append(["d", "coverage_pct", r["month"], f"{r['coverage']:.4f}", r["eligible"]])
for mo in MODELS:
    for m in months:
        rows.append(["e", f"{mo}/fit_ms", m, f"{TIM[(mo, m)]['fit_ms']:.6f}", n_month[m][mo]])
        rows.append(["e", f"{mo}/predict_ms", m, f"{TIM[(mo, m)]['pred_ms']:.6f}", n_month[m][mo]])
with open(FIG / "figure_data.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["panel", "series", "month", "value", "n"])
    w.writerows(rows)

# ------------------------------------------------------------------- figure
plt.rcParams.update({
    "font.family": "serif", "font.size": 7.4, "axes.titlesize": 8.2,
    "axes.labelsize": 7.4, "xtick.labelsize": 6.8, "ytick.labelsize": 6.8,
    "legend.fontsize": 6.6, "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 200, "savefig.dpi": 300, "hatch.linewidth": 0.6,
})
XT = [f"{m[:4]}-{m[5:]}" for m in months]
fig = plt.figure(figsize=(6.5, 7.9))
gs = GridSpec(3, 2, figure=fig, hspace=0.80, wspace=0.55,
              height_ratios=[1.0, 1.0, 0.95])
ax_a, ax_b = fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1])
ax_c, ax_d = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
ax_e = fig.add_subplot(gs[2, :])
# right margin leaves room for panel (d)'s twin axis ticks and axis label
fig.subplots_adjust(left=0.115, right=0.900, top=0.935, bottom=0.085)

# (a) primary comparison: pooled error, policy effect within each form
x = np.arange(len(MODELS))
bw = 0.34
for j, po in enumerate(POLICIES):
    vals = [float(O[(mo, po)]["rmse"]) for mo in MODELS]
    bars = ax_a.bar(x + (j - 0.5) * bw, vals, bw * 0.92,
                    color=[C[mo] for mo in MODELS],
                    alpha=0.34 if po == FROZEN else 0.92,
                    hatch="////" if po == FROZEN else None,
                    edgecolor="white", linewidth=0.5, zorder=3)
    for b, v in zip(bars, vals):
        ax_a.text(b.get_x() + b.get_width() / 2, v * 1.10, f"{v:.3f}",
                  ha="center", va="bottom", fontsize=6.8, fontweight="bold")
ax_a.set_yscale("log")
ax_a.set_ylim(0.05, 9.0)
ax_a.set_xticks(x)
ax_a.set_xticklabels(["Linear form", "Quadratic form"])
ax_a.set_ylabel("Pooled RMSE (log scale)")
ax_a.set_title("(a) Which change matters: form or update policy", loc="left")
ax_a.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
ax_a.yaxis.set_minor_formatter(NullFormatter())
for i, mo in enumerate(MODELS):
    ax_a.annotate("", xy=(i + 0.5 * bw, float(O[(mo, UPDATED)]["rmse"])),
                  xytext=(i - 0.5 * bw, float(O[(mo, FROZEN)]["rmse"])),
                  arrowprops=dict(arrowstyle="-|>", lw=0.7, color="#444444",
                                  shrinkA=1, shrinkB=1), zorder=4)
    # the arrow shows the direction of the policy effect; the pooled size is
    # reported in the note beneath the axis so that no text sits on the bars
ax_a.legend(handles=[Patch(facecolor="#777777", alpha=0.34, hatch="////", label="frozen calibration"),
                     Patch(facecolor="#777777", alpha=0.92, label="monthly-updated")],
            loc="upper center", bbox_to_anchor=(0.5, -0.20), ncol=2, frameon=False,
            handlelength=1.4, columnspacing=1.2)
ax_a.text(0.5, -0.38,
          f"monthly updates vs frozen: −{rel[LINEAR]['rel_rmse']:.0f}% pooled RMSE (linear), "
          f"−{rel[QUAD]['rel_rmse']:.0f}% (quadratic)",
          transform=ax_a.transAxes, ha="center", va="top", fontsize=6.2, color="#333333")
ax_a.text(0.99, 0.02, f"n = {n_tot:,} scored cases", transform=ax_a.transAxes,
          ha="right", va="bottom", fontsize=6.2, color="#555555")

# (b) monthly pooled-RMSE trajectories
for mo in MODELS:
    for po in POLICIES:
        ys = [float(M[(m, mo, po)]["rmse"]) for m in months]
        ax_b.plot(range(len(months)), ys, marker="o" if po == UPDATED else "s",
                  ms=3.0, lw=1.15, color=C[mo],
                  ls="-" if po == UPDATED else "--",
                  alpha=0.95 if po == UPDATED else 0.6,
                  label=f"{mo}, {po}", zorder=3)
ax_b.set_yscale("log")
ax_b.set_xticks(range(len(months)))
ax_b.set_xticklabels(XT, rotation=20, ha="right")
ax_b.set_ylabel("Monthly RMSE (log)")
ax_b.set_title("(b) Every month, both forms", loc="left")
ax_b.set_xlim(-0.45, len(months) - 0.55)
ax_b.axvspan(len(months) - 1.45, len(months) - 0.55, color="#d9d9d9", alpha=0.55, zorder=0)
ax_b.text(len(months) - 1.0, ax_b.get_ylim()[1] * 0.55, "87 h", ha="center",
          va="top", fontsize=6.0, color="#444444", rotation=90)
ax_b.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
ax_b.yaxis.set_minor_formatter(NullFormatter())
ax_b.legend(loc="upper center", bbox_to_anchor=(0.5, -0.30), ncol=2, frameon=False,
            handlelength=1.6, columnspacing=1.0)

# (c) relative change per month (the policy effect, month by month)
xw = np.arange(len(months))
for j, mo in enumerate(MODELS):
    vals = [100 * (float(M[(m, mo, UPDATED)]["rmse"]) - float(M[(m, mo, FROZEN)]["rmse"]))
            / float(M[(m, mo, FROZEN)]["rmse"]) for m in months]
    bars = ax_c.bar(xw + (j - 0.5) * 0.38, vals, 0.36, color=C[mo], label=mo,
                    alpha=0.9, edgecolor="white", linewidth=0.5, zorder=3)
    for b, v in zip(bars, vals):
        # offset in points (not data units) and rotated, so the two labels per
        # month can never run into each other when the values are close
        ax_c.annotate(f"{v:+.0f}", (b.get_x() + b.get_width() / 2, v),
                      textcoords="offset points", xytext=(0, 2.5 if v >= 0 else -2.5),
                      ha="center", va="bottom" if v >= 0 else "top", rotation=90,
                      fontsize=5.4, clip_on=True, zorder=6)
ax_c.axhline(0, color="#333333", lw=0.8, zorder=2)
# limits follow the data: the largest monthly drop is about -63%, so a fixed
# -28 floor used to clip those bars and push their labels into panel (e)
c_pct = {mo: [100 * (float(M[(m, mo, UPDATED)]["rmse"])
                   - float(M[(m, mo, FROZEN)]["rmse"]))
              / float(M[(m, mo, FROZEN)]["rmse"]) for m in months] for mo in MODELS}
c_min = min(min(v) for v in c_pct.values())
c_max = max(max(v) for v in c_pct.values())
ax_c.set_ylim(c_min - 10.0, max(c_max, 0.0) + 11.0)
ax_c.set_xticks(xw)
ax_c.set_xticklabels(XT, rotation=20, ha="right")
ax_c.set_ylabel("ΔRMSE from updating (%)")
ax_c.set_title("(c) Updating helps in every full month", loc="left")
ax_c.legend(loc="upper center", bbox_to_anchor=(0.5, -0.30), ncol=2, frameon=False,
            handlelength=1.2, columnspacing=1.4)
ax_c.text(0.015, 0.05, "negative = updated better", transform=ax_c.transAxes,
          fontsize=6.0, color="#555555")

# (d) coverage: growing fit window vs share of test hours that are eligible
ax_d.bar(xw, [r["fit_n"] / 1000 for r in cov], 0.62, color="#8c8c8c", alpha=0.45,
         edgecolor="#5a5a5a", linewidth=0.5, zorder=2)
ax_d.set_ylabel("Fit-window rows (thousands)")
# headroom above the tallest bar keeps the bar labels clear of the coverage line
ax_d.set_ylim(0, 13.5)
ax_d2 = ax_d.twinx()
ax_d2.plot(xw, [r["coverage"] for r in cov], color="#7d1f3f", marker="D", ms=3.2,
           lw=1.1, zorder=5)
# padded range plus round ticks: no tick label sits on the panel edge
ax_d2.set_ylim(80, 114)
ax_d2.set_yticks([85, 90, 95, 100])
ax_d2.set_ylabel("Eligible share (%)")
ax_d2.tick_params(axis="y", pad=2.0, labelsize=6.6)
ax_d2.spines["top"].set_visible(False)
ax_d2.spines["right"].set_visible(True)
for i, r in enumerate(cov):
    ax_d.text(i, r["fit_n"] / 1000 + 0.22, f"{r['fit_n'] / 1000:.1f}k", ha="center",
              va="bottom", fontsize=6.0, color="#3d3d3d", zorder=6)
    # coverage labels sit above their marker, in the free band under the top
    ax_d2.annotate(f"{r['coverage']:.0f}%", (i, r["coverage"]), textcoords="offset points",
                   xytext=(0, 7), ha="center", va="bottom", fontsize=5.8,
                   color="#7d1f3f", zorder=7)
ax_d.set_xticks(xw)
ax_d.set_xticklabels(XT, rotation=20, ha="right")
ax_d.set_title("(d) Fit window and coverage", loc="left")
handles = [Patch(facecolor="#8c8c8c", alpha=0.45, edgecolor="#5a5a5a", label="rows in fit window"),
           plt.Line2D([], [], color="#7d1f3f", marker="D", ms=3.2, lw=1.1,
                      label="eligible / available test hours")]
ax_d.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.30), ncol=1,
            frameon=False, handlelength=1.6)
ax_d2.set_zorder(ax_d.get_zorder() + 1)
ax_d2.patch.set_visible(False)

# (e) cost of one monthly refit, both forms
for j, mo in enumerate(MODELS):
    fit = [TIM[(mo, m)]["fit_ms"] for m in months]
    pred = [TIM[(mo, m)]["pred_ms"] for m in months]
    ax_e.bar(xw + (j - 0.5) * 0.36, fit, 0.34, color=C[mo], alpha=0.55,
             edgecolor="white", linewidth=0.5, zorder=2)
    ax_e.plot(xw + (j - 0.5) * 0.36, pred, linestyle="none", marker="D", ms=3.0,
              color=C[mo], markeredgecolor="white", markeredgewidth=0.4, zorder=5)
ax_e.set_yscale("log")
ax_e.set_ylim(0.2, 130)
ax_e.set_xticks(xw)
ax_e.set_xticklabels(XT, rotation=0)
ax_e.set_ylabel("Seconds per run (ms, log)")
ax_e.set_yticks([0.25, 1, 5, 20, 100])
ax_e.set_yticklabels(["0.25", "1", "5", "20", "100"])
ax_e.yaxis.set_minor_formatter(NullFormatter())
ax_e.set_title("(e) One refit costs milliseconds; the update itself is cheap", loc="left")
handles = [Patch(facecolor=C[mo], alpha=0.55, label=f"{mo}: fit (median of repeats)")
           for mo in MODELS] + [
    plt.Line2D([], [], color="#333333", marker="D", ms=3.0, lw=0,
               label="prediction pass over the month's test cohort")]
ax_e.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.26), ncol=3,
            frameon=False, handlelength=1.5, columnspacing=1.1)
ax_e.text(0.995, 0.93,
          f"six refits: {1000 * TOT[(LINEAR, 'fit')]:.1f} ms linear, "
          f"{1000 * TOT[(QUAD, 'fit')]:.0f} ms quadratic\n"
          f"twelve refits (annualised): {1000 * ANNUAL[LINEAR]:.0f} ms / "
          f"{1000 * ANNUAL[QUAD]:.0f} ms\n"
          f"amortised over {n_tot:,} test hours: "
          f"{AMORT[LINEAR]:.1f} / {AMORT[QUAD]:.1f} µs per hour",
          transform=ax_e.transAxes, ha="right", va="top", fontsize=6.0,
          bbox=dict(boxstyle="round,pad=0.32", facecolor="white", edgecolor="#cccccc",
                    linewidth=0.5))

# layout evidence: no legend or annotation box may overlap any axes' data area
fig.canvas.draw()
ax_rects = [(a, a.get_window_extent()) for a in (ax_a, ax_b, ax_c, ax_d, ax_e)]


def overlap(r1, r2):
    dx = min(r1.x1, r2.x1) - max(r1.x0, r2.x0)
    dy = min(r1.y1, r2.y1) - max(r1.y0, r2.y0)
    return dx * dy if dx > 0 and dy > 0 else 0.0


problems = []
for a, rect in ax_rects:
    leg = a.get_legend()
    if leg is None:
        continue
    lr = leg.get_window_extent()
    for b, orect in ax_rects:
        area = overlap(lr, orect)
        if area > 25:  # px^2 tolerance
            problems.append(f"legend of {a.get_title(loc='left')[:12]} overlaps an axes ({area:.0f}px^2)")
check(not problems, "figure legends sit outside every data area" if not problems else "; ".join(problems))

# panel (c): every bar value label must lie inside its own axes
axc = ax_c.get_window_extent()
c_labels = [t for t in ax_c.texts if t.get_text().startswith(("+", "-", "0"))]
check(len(c_labels) == len(MODELS) * len(months),
      f"panel (c) labelled every monthly bar ({len(c_labels)} labels)")
spill = [t.get_text() for t in c_labels
         if not (axc.y0 - 1 <= t.get_window_extent().y0
                 and t.get_window_extent().y1 <= axc.y1 + 1)]
check(not spill, "panel (c) value labels stay inside their axes"
      if not spill else f"panel (c) labels outside axes: {spill}")

# panel (d): right-hand tick labels and axis label must fit on the canvas
canvas_w = fig.get_window_extent().width
right_most = max([t.get_window_extent().x1 for t in ax_d2.get_yticklabels()]
                 + [ax_d2.yaxis.label.get_window_extent().x1])
check(right_most <= canvas_w - 1,
      f"panel (d) right-hand labels inside the canvas ({right_most:.0f} <= {canvas_w:.0f} px)")

fig.savefig(FIG / "monthly_update_panel.png", bbox_inches=None, facecolor="white")
fig.savefig(FIG / "monthly_update_panel.pdf", facecolor="white")
plt.close(fig)
check((FIG / "monthly_update_panel.png").stat().st_size > 40_000,
      "figure PNG written with plausible content")

# ------------------------------------------------------------- derived metrics
derived = {
    "scope": {
        "test_hours": n_tot, "months": months,
        "test_period": [periods["test"]["eligible_first"], periods["test"]["eligible_last"]],
        "fit_window_first": int(cohorts_csv[0]["fit_n"]),
        "fit_window_last": int(cohorts_csv[-1]["fit_n"]),
        "source": "saved calibration results (recorded_results.json + reference CSVs)",
        "refits_run_here": 0,
    },
    "pooled": {f"{mo}/{po}": {k: float(O[(mo, po)][k]) for k in
                              ("n", "rmse", "mae", "bias", "p95_abs_error", "r2",
                               "negative_prediction_count")}
               for mo in MODELS for po in POLICIES},
    "policy_effect": rel,
    "form_gap_quad_frozen_over_linear_updated": form_gap,
    "monthly_delta": {f"{m}/{mo}": {
        "n": int(float(M[(m, mo, FROZEN)]["n"])),
        "rmse_frozen": float(M[(m, mo, FROZEN)]["rmse"]),
        "rmse_updated": float(M[(m, mo, UPDATED)]["rmse"]),
        "delta_rmse": float(M[(m, mo, UPDATED)]["rmse"]) - float(M[(m, mo, FROZEN)]["rmse"]),
        "pct_rmse": 100 * (float(M[(m, mo, UPDATED)]["rmse"]) - float(M[(m, mo, FROZEN)]["rmse"]))
        / float(M[(m, mo, FROZEN)]["rmse"]),
    } for m in months for mo in MODELS},
    "coverage": cov,
    "timing": {
        "per_month_ms": {f"{mo}/{m}": {"fit": TIM[(mo, m)]["fit_ms"],
                                       "predict": TIM[(mo, m)]["pred_ms"]}
                         for mo in MODELS for m in months},
        "fit_median_ms_range": {mo: [min(TIM[(mo, m)]["fit_ms"] for m in months),
                                     max(TIM[(mo, m)]["fit_ms"] for m in months)]
                                for mo in MODELS},
        "predict_median_ms_range": {mo: [min(TIM[(mo, m)]["pred_ms"] for m in months),
                                         max(TIM[(mo, m)]["pred_ms"] for m in months)]
                                    for mo in MODELS},
        "six_refit_total_s": {mo: TOT[(mo, "fit")] for mo in MODELS},
        "six_prediction_total_s": {mo: TOT[(mo, "prediction")] for mo in MODELS},
        "annualised_fit_s": ANNUAL,
        "us_per_test_hour": AMORT,
        "recorded_pred_us_per_row": {f"{mo}/{m}": TIM[(mo, m)]["pred_us_per_row"]
                                     for mo in MODELS for m in months},
        "implied_pred_batch_rows": {f"{mo}/{m}": TIM[(mo, m)]["pred_rows_implied"]
                                    for mo in MODELS for m in months},
        "scope_note": ("costs are medians of repeated in-process timings; they exclude "
                       "hourly ingestion, feature construction, evaluation and plotting"),
    },
    "checks": {"failures": FAIL, "warnings": WARN},
}
(HERE / "derived_metrics.json").write_text(json.dumps(derived, indent=2), encoding="utf-8")

# ------------------------------------------------------------- numbers block
L = []
L.append("REPORT NUMBERS (printed last so the tail is readable)")
L.append(f"test hours {n_tot}, months {months[0]}..{months[-1]}, fit rows "
         f"{cov[0]['fit_n']}..{cov[-1]['fit_n']}")
for mo in MODELS:
    f, u = O[(mo, FROZEN)], O[(mo, UPDATED)]
    L.append(f"{mo:9s} frozen rmse {float(f['rmse']):.4f} mae {float(f['mae']):.4f} "
             f"|b| {abs(float(f['bias'])):.4f} p95 {float(f['p95_abs_error']):.4f} "
             f"r2 {float(f['r2']):.4f} neg {int(float(f['negative_prediction_count']))}")
    L.append(f"{'':9s} updated rmse {float(u['rmse']):.4f} mae {float(u['mae']):.4f} "
             f"|b| {abs(float(u['bias'])):.4f} p95 {float(u['p95_abs_error']):.4f} "
             f"r2 {float(u['r2']):.4f} neg {int(float(u['negative_prediction_count']))}")
    L.append(f"{'':9s} ratio {rel[mo]['ratio']:.3f}x  rel_rmse {rel[mo]['rel_rmse']:.1f}%  "
             f"rel_p95 {rel[mo]['rel_p95']:.1f}%  rel_mae {rel[mo]['rel_mae']:.1f}%")
L.append(f"quadratic frozen / linear updated = {form_gap:.2f}x")
for m in months:
    L.append(f"{m} n={n_month[m][LINEAR]:4d} | lin {float(M[(m, LINEAR, FROZEN)]['rmse']):.3f}"
             f"->{float(M[(m, LINEAR, UPDATED)]['rmse']):.3f} "
             f"({100 * (float(M[(m, LINEAR, UPDATED)]['rmse']) - float(M[(m, LINEAR, FROZEN)]['rmse'])) / float(M[(m, LINEAR, FROZEN)]['rmse']):+.1f}%)"
             f" | quad {float(M[(m, QUAD, FROZEN)]['rmse']):.3f}->"
             f"{float(M[(m, QUAD, UPDATED)]['rmse']):.3f} "
             f"({100 * (float(M[(m, QUAD, UPDATED)]['rmse']) - float(M[(m, QUAD, FROZEN)]['rmse'])) / float(M[(m, QUAD, FROZEN)]['rmse']):+.1f}%)")
for r in cov:
    L.append(f"{r['month']} fit_n {r['fit_n']:5d} avail {r['available']:4d} elig "
             f"{r['eligible']:4d} excl {r['excluded']:3d} cov {r['coverage']:5.1f}% "
             f"share {r['share_test']:4.1f}%")
for mo in MODELS:
    L.append(f"{mo} fit medians ms " +
             ", ".join(f"{TIM[(mo, m)]['fit_ms']:.2f}" for m in months))
    L.append(f"{mo} predict medians ms " +
             ", ".join(f"{TIM[(mo, m)]['pred_ms']:.3f}" for m in months))
    L.append(f"{mo} totals: 6 refits {1000 * TOT[(mo, 'fit')]:.2f} ms, 6 pred passes "
             f"{1000 * TOT[(mo, 'prediction')]:.2f} ms, annualised fit "
             f"{1000 * ANNUAL[mo]:.1f} ms, amortised {AMORT[mo]:.2f} us/test-hour")
    L.append(f"{mo} recorded pred us/row " +
             ", ".join(f"{TIM[(mo, m)]['pred_us_per_row']:.2f}" for m in months))
    L.append(f"{mo} implied pred batch rows " +
             ", ".join(f"{TIM[(mo, m)]['pred_rows_implied']:.0f}" for m in months))
L.append(f"warnings: {WARN}")
print("\n".join(L))
check(not FAIL, "all consistency checks passed")
if FAIL:
    sys.exit(1)
print("OK: tables, figure and derived metrics written")
