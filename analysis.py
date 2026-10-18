"""
Summary tables and PDF plots generated from a results directory.

Inputs (written by run_experiments.py):
    <out>/runs.jsonl              one JSON record per run (metadata, status)
    <out>/samples/<run_id>.csv    per-sample TCP_INFO + goodput log

Outputs:
    <out>/tables/goodput_summary.{csv,md}       Q1(c): min/median/mean/p95 per dest x algorithm
    <out>/tables/run_summary.{csv,md}           per-run statistics (RTT, cwnd, loss, BDP)
    <out>/tables/algorithm_comparison.{csv,md}  Q3(a): mean +- std across repetitions
    <out>/plots/q1_goodput_timeseries.pdf       goodput over time, every destination
    <out>/plots/q2_timeseries_<alg>.pdf         Q2(b)(i)  (one per algorithm, Q3 repeats them)
    <out>/plots/q2_scatter_<alg>.pdf            Q2(b)(ii)
    <out>/plots/q3_timeseries_compare.pdf       algorithms side by side, shared axes
    <out>/plots/q3_timeseries_overlay.pdf       algorithms overlaid, shared axes
    <out>/plots/q3_scatter_compare.pdf          scatter plots side by side, shared axes
    <out>/plots/q3_regimes.pdf                  per-destination comparison across runs
    <out>/tables/aggregate_by_algorithm.{csv,md} A3: throughput and RTT aggregate statistics per algorithm

Assignment 3 runs four algorithms: cubic, reno, bbr and gentle_reno (our eBPF
implementation in bpf/gentle_reno.bpf.c). The Q2(b)-style figures
(q2_timeseries_<alg>.pdf, q2_scatter_<alg>.pdf, q3_*_compare.pdf) are produced for
all four with the same representative destination and shared axis scales.
"""

import csv
import json
import math
import os

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

USABLE = ("ok", "incomplete_termination")

# Fixed categorical colours (validated colour-blind-safe order) + secondary
# encodings (line style, marker) so identity never depends on colour alone.
ALG_STYLE = {
    "cubic": dict(color="#2a78d6", ls="-", marker="o"),
    "reno": dict(color="#eb6834", ls="--", marker="s"),
    "bbr": dict(color="#1baf7a", ls=":", marker="^"),
    "gentle_reno": dict(color="#9b59d0", ls="-.", marker="D"),
}
_EXTRA = [dict(color="#eda100", ls="-.", marker="D"), dict(color="#e87ba4", ls="-", marker="v"),
          dict(color="#008300", ls="--", marker="P")]
INK, MUTED, GRID = "#222222", "#6b6b6b", "#e4e4e4"

plt.rcParams.update({
    "font.size": 8.5, "axes.titlesize": 9, "axes.labelsize": 8.5,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
    "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "lines.linewidth": 1.2, "pdf.fonttype": 42,
})


def style(alg, i=0):
    if alg in ALG_STYLE:
        return ALG_STYLE[alg]
    return _EXTRA[i % len(_EXTRA)]


# ------------------------------------------------------------------ loading
def _num(v):
    if v in (None, "", "None"):
        return math.nan
    try:
        return float(v)
    except ValueError:
        return math.nan


def load_runs(out_dir):
    runs = []
    path = os.path.join(out_dir, "runs.jsonl")
    if not os.path.exists(path):
        return runs
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs


def load_samples(out_dir, run_id):
    path = os.path.join(out_dir, "samples", run_id + ".csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    cols = {}
    for k in rows[0]:
        vals = [r[k] for r in rows]
        if k in ("run_id", "destination", "algorithm", "algorithm_actual"):
            cols[k] = np.array(vals, dtype=object)
        else:
            cols[k] = np.array([_num(v) for v in vals], dtype=float)
    return cols


def derived(s):
    """Plot-ready series in convenient units (interval rows only, i.e. idx>0)."""
    m = ~np.isnan(s["goodput_bps"])
    d = {
        "t": s["t_s"][m],
        "goodput_mbps": s["goodput_bps"][m] / 1e6,
        "cwnd_seg": s["snd_cwnd_seg"][m],
        "ssthresh_seg": s["snd_ssthresh_seg"][m],
        "srtt_ms": s["srtt_us"][m] / 1e3,
        "rttvar_ms": s["rttvar_us"][m] / 1e3,
        "min_rtt_ms": s["min_rtt_us"][m] / 1e3,
        "d_retrans": s["d_retrans_seg"][m],
        "delivery_mbps": s["delivery_rate_Bps"][m] * 8 / 1e6,
        "pacing_mbps": s["pacing_rate_Bps"][m] * 8 / 1e6,
    }
    return d


def _warm(d, warmup):
    keep = d["t"] >= warmup
    return {k: v[keep] for k, v in d.items()}


# ------------------------------------------------------------------ tables
def _write_table(rows, columns, base_path, title=None, floatfmt="%.2f"):
    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    with open(base_path + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    def fmt(v):
        if isinstance(v, float):
            return "" if math.isnan(v) else floatfmt % v
        return "" if v is None else str(v)

    with open(base_path + ".md", "w") as f:
        if title:
            f.write("### %s\n\n" % title)
        f.write("| " + " | ".join(columns) + " |\n")
        f.write("|" + "|".join("---" for _ in columns) + "|\n")
        for r in rows:
            f.write("| " + " | ".join(fmt(r.get(c)) for c in columns) + " |\n")


def _pct(a, q):
    return float(np.percentile(a, q)) if len(a) else math.nan


def _stat(fn, a):
    return float(fn(a)) if len(a) else math.nan


def run_stats(run, s, warmup=0.0):
    d = _warm(derived(s), warmup)
    g = d["goodput_mbps"]
    srtt = d["srtt_ms"][~np.isnan(d["srtt_ms"])]
    min_rtt_ms = np.nanmin(d["min_rtt_ms"]) if len(d["min_rtt_ms"]) else math.nan
    sent = s["bytes_sent_B"]
    retr = s["bytes_retrans_B"]
    total_sent = np.nanmax(sent) if len(sent) else math.nan
    total_retr = np.nanmax(retr) if len(retr) else math.nan
    # BDP estimate: bottleneck rate (max delivery-rate sample, robust p95 of goodput
    # as a fallback) x propagation delay (min RTT).
    btlbw_mbps = np.nanmax(d["delivery_mbps"]) if len(d["delivery_mbps"]) else math.nan
    if math.isnan(btlbw_mbps):
        btlbw_mbps = _pct(g, 95)
    bdp_kB = btlbw_mbps * 1e6 / 8 * min_rtt_ms / 1e3 / 1e3
    mss = np.nanmedian(s["snd_mss_B"])
    mean_cwnd_kB = np.nanmean(d["cwnd_seg"]) * mss / 1e3 if len(d["cwnd_seg"]) else math.nan
    return {
        "run_id": run["run_id"], "rep": run["rep"], "destination": run["destination"],
        "algorithm": run["algorithm"], "algorithm_actual": run.get("actual_cc"),
        "status": run["status"], "duration_s": run.get("duration_actual_s"),
        "samples": int(len(g)),
        "goodput_mean_mbps": _stat(np.mean, g), "goodput_median_mbps": _stat(np.median, g),
        "goodput_p95_mbps": _pct(g, 95),
        "min_rtt_ms": min_rtt_ms, "srtt_mean_ms": _stat(np.mean, srtt),
        "srtt_p95_ms": _pct(srtt, 95),
        "rtt_inflation_ms": _stat(np.mean, srtt) - min_rtt_ms,
        "cwnd_mean_seg": _stat(np.nanmean, d["cwnd_seg"]),
        "cwnd_mean_kB": mean_cwnd_kB,
        "retrans_total_seg": float(np.nanmax(s["total_retrans_seg"])),
        "rto_total": (float(np.nanmax(s["total_rto"])) if "total_rto" in s
                      and np.isfinite(s["total_rto"]).any() else math.nan),
        "retrans_rate_pct": (100.0 * total_retr / total_sent) if total_sent else math.nan,
        "bdp_est_kB": bdp_kB,
        "cwnd_over_bdp": mean_cwnd_kB / bdp_kB if bdp_kB else math.nan,
        "server_rx_MB": (run.get("server_bytes_received_B") or math.nan) / 1e6,
        "client_acked_MB": float(np.nanmax(s["bytes_acked_B"])) / 1e6,
    }


def build_tables(out_dir, runs, samples, warmup=0.0):
    tdir = os.path.join(out_dir, "tables")
    usable = [r for r in runs if r["status"] in USABLE and samples.get(r["run_id"]) is not None]

    # Per-run summary
    per_run = [run_stats(r, samples[r["run_id"]], warmup) for r in usable]
    cols = list(per_run[0].keys()) if per_run else ["run_id"]
    _write_table(per_run, cols, os.path.join(tdir, "run_summary"), "Per-run summary")

    # Q1(c): goodput distribution over all interval samples of all repetitions
    algs = _algorithms(runs)
    dests = _destinations(runs)
    q1 = []
    for dest in dests:
        for alg in algs:
            rs = [r for r in usable if r["destination"] == dest and r["algorithm"] == alg]
            if not rs:
                continue
            g = np.concatenate([_warm(derived(samples[r["run_id"]]), warmup)["goodput_mbps"]
                                for r in rs])
            q1.append({
                "destination": dest, "algorithm": alg,
                "algorithm_actual": ",".join(sorted({str(r.get("actual_cc")) for r in rs})),
                "runs": len(rs), "samples": int(len(g)),
                "min_mbps": _stat(np.min, g), "median_mbps": _stat(np.median, g),
                "mean_mbps": _stat(np.mean, g), "p95_mbps": _pct(g, 95),
            })
    _write_table(q1, ["destination", "algorithm", "algorithm_actual", "runs", "samples",
                      "min_mbps", "median_mbps", "mean_mbps", "p95_mbps"],
                 os.path.join(tdir, "goodput_summary"),
                 "Goodput per destination and algorithm (Mbit/s, all interval samples)")

    # Q3(a): mean +- std across repetitions of the per-run means
    comp = []
    for dest in dests + ["ALL"]:
        row = {"destination": dest}
        best = {}
        for alg in algs:
            rs = [p for p in per_run if p["algorithm"] == alg and (dest == "ALL" or p["destination"] == dest)]
            g = np.array([p["goodput_mean_mbps"] for p in rs])
            rtt = np.array([p["srtt_mean_ms"] for p in rs])
            rr = np.array([p["retrans_rate_pct"] for p in rs])
            row["%s_runs" % alg] = len(rs)
            row["%s_goodput_mbps" % alg] = _stat(np.mean, g)
            row["%s_goodput_std" % alg] = _stat(np.std, g)
            row["%s_srtt_ms" % alg] = _stat(np.mean, rtt)
            row["%s_srtt_std" % alg] = _stat(np.std, rtt)
            row["%s_retrans_pct" % alg] = _stat(np.mean, rr)
            if len(rs):
                best[alg] = (row["%s_goodput_mbps" % alg], row["%s_srtt_ms" % alg])
        if best:
            row["best_goodput"] = max(best, key=lambda a: best[a][0])
            row["lowest_rtt"] = min(best, key=lambda a: best[a][1])
        comp.append(row)
    ccols = ["destination"]
    for alg in algs:
        ccols += ["%s_runs" % alg, "%s_goodput_mbps" % alg, "%s_goodput_std" % alg,
                  "%s_srtt_ms" % alg, "%s_srtt_std" % alg, "%s_retrans_pct" % alg]
    ccols += ["best_goodput", "lowest_rtt"]
    _write_table(comp, ccols, os.path.join(tdir, "algorithm_comparison"),
                 "Algorithm comparison: mean (std) over repetitions of per-run means")

    # A3: aggregate throughput / RTT statistics per algorithm over every
    # interval sample of every usable run (all destinations, all repetitions).
    agg = []
    for alg in algs:
        rs = [r for r in usable if r["algorithm"] == alg]
        if not rs:
            continue
        ds = [_warm(derived(samples[r["run_id"]]), warmup) for r in rs]
        g = np.concatenate([d["goodput_mbps"] for d in ds])
        rtt = np.concatenate([d["srtt_ms"] for d in ds])
        rtt = rtt[np.isfinite(rtt)]
        pr = [p for p in per_run if p["algorithm"] == alg]
        agg.append({
            "algorithm": alg,
            "algorithm_actual": ",".join(sorted({str(r.get("actual_cc")) for r in rs})),
            "runs": len(rs), "destinations": len({r["destination"] for r in rs}),
            "samples": int(len(g)),
            "goodput_mean_mbps": _stat(np.mean, g), "goodput_median_mbps": _stat(np.median, g),
            "goodput_p5_mbps": _pct(g, 5), "goodput_p95_mbps": _pct(g, 95),
            "srtt_mean_ms": _stat(np.mean, rtt), "srtt_median_ms": _stat(np.median, rtt),
            "srtt_p95_ms": _pct(rtt, 95),
            "rtt_inflation_mean_ms": _stat(np.nanmean, np.array([p["rtt_inflation_ms"] for p in pr])),
            "retrans_rate_mean_pct": _stat(np.nanmean, np.array([p["retrans_rate_pct"] for p in pr])),
            "retrans_per_run_mean": _stat(np.nanmean, np.array([p["retrans_total_seg"] for p in pr])),
            "rto_per_run_mean": _stat(np.nanmean, np.array([p["rto_total"] for p in pr])),
        })
    _write_table(agg, list(agg[0].keys()) if agg else ["algorithm"],
                 os.path.join(tdir, "aggregate_by_algorithm"),
                 "Aggregate throughput and RTT per algorithm (all destinations and repetitions)")
    return per_run, q1, comp


# ------------------------------------------------------------------ helpers
def _algorithms(runs):
    seen = []
    for r in runs:
        if r["algorithm"] not in seen:
            seen.append(r["algorithm"])
    return seen


def _destinations(runs):
    seen = []
    for r in sorted(runs, key=lambda r: r.get("dest_index", 0)):
        if r["destination"] not in seen:
            seen.append(r["destination"])
    return seen


def choose_representative(runs, samples, override=None):
    """Destination with complete data for every algorithm (most usable runs),
    tie-broken by being most 'typical' (median goodput closest to the median
    over destinations). Returns (destination, rep)."""
    algs = _algorithms(runs)
    usable = [r for r in runs if r["status"] in USABLE and samples.get(r["run_id"]) is not None]
    dests = _destinations(runs)
    if override:
        cand = [d for d in dests if d == override or d.split(":")[0] == override]
        if not cand:
            raise SystemExit("--rep-dest %r not among destinations %s" % (override, dests))
        dests = cand

    def score(dest):
        counts = [sum(1 for r in usable if r["destination"] == dest and r["algorithm"] == a) for a in algs]
        return min(counts), sum(counts)

    scored = [(score(d), d) for d in dests]
    if not scored:
        return None, None
    top = max(s for s, _ in scored)
    if top[0] == 0 and not override:
        print("WARNING: no destination has usable runs for every algorithm")
    finalists = [d for s, d in scored if s == top]
    med = {d: np.median([np.nanmean(derived(samples[r["run_id"]])["goodput_mbps"])
                         for r in usable if r["destination"] == d] or [math.nan]) for d in finalists}
    overall = np.nanmedian(list(med.values()))
    dest = min(finalists, key=lambda d: abs(med[d] - overall) if not math.isnan(med[d]) else 1e18)

    reps = sorted({r["rep"] for r in usable if r["destination"] == dest})
    rep = None
    for k in reps:
        if all(any(r["rep"] == k and r["destination"] == dest and r["algorithm"] == a for r in usable)
               for a in algs):
            rep = k
            break
    if rep is None and reps:
        rep = reps[0]
    return dest, rep


def _get_run(runs, dest, alg, rep):
    for r in runs:
        if r["destination"] == dest and r["algorithm"] == alg and r["rep"] == rep and r["status"] in USABLE:
            return r
    for r in runs:  # fall back to any usable repetition
        if r["destination"] == dest and r["algorithm"] == alg and r["status"] in USABLE:
            return r
    return None


def _limits(values, pad=0.05, floor0=True):
    v = np.concatenate([np.asarray(x, dtype=float).ravel() for x in values if len(x)]) if values else np.array([])
    v = v[np.isfinite(v)]
    if not len(v):
        return None
    lo, hi = (0.0 if floor0 else float(v.min())), float(v.max())
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    return (lo if floor0 else lo - pad * span, hi + pad * span)


def _ssthresh_valid(ss):
    ss = ss.copy()
    ss[ss >= 0x7FFFFFFF - 1] = np.nan  # TCP_INFINITE_SSTHRESH = "not set yet"
    return ss


# ------------------------------------------------------------------ plots
def _ts_axes_fill(axes, d, alg, i=0, lim=None, label=None, show_band=True):
    st = style(alg, i)
    c = st["color"]
    ax = axes[0]
    ax.plot(d["t"], d["cwnd_seg"], color=c, ls=st["ls"], label=label or "snd_cwnd")
    ss = _ssthresh_valid(d["ssthresh_seg"])
    if np.isfinite(ss).any() and label is None:
        ax.plot(d["t"], ss, color=MUTED, lw=0.9, ls=(0, (4, 2)), label="snd_ssthresh")
    ax = axes[1]
    ax.plot(d["t"], d["srtt_ms"], color=c, ls=st["ls"], label=label or "srtt")
    if show_band:
        ax.fill_between(d["t"], d["srtt_ms"] - d["rttvar_ms"], d["srtt_ms"] + d["rttvar_ms"],
                        color=c, alpha=0.15, lw=0, label="srtt ± rttvar")
        ax.plot(d["t"], d["min_rtt_ms"], color=MUTED, lw=0.9, ls=(0, (4, 2)), label="min_rtt")
    ax = axes[2]
    if label is None:
        dt = np.median(np.diff(d["t"])) if len(d["t"]) > 1 else 0.2
        ax.bar(d["t"], d["d_retrans"], width=dt * 0.85, color=c, align="center",
               label="retransmitted segments / interval")
    else:
        ax.plot(d["t"], d["d_retrans"], color=c, ls=st["ls"], lw=1.0, label=label)
    ax = axes[3]
    ax.plot(d["t"], d["goodput_mbps"], color=c, ls=st["ls"], label=label or "goodput (Δbytes_acked)")
    if label is None and np.isfinite(d["delivery_mbps"]).any():
        ax.plot(d["t"], d["delivery_mbps"], color=MUTED, lw=0.8, ls=":", label="kernel delivery_rate")


TS_YLABELS = ["snd_cwnd [segments]", "RTT [ms]", "retransmits\n[segments/interval, symlog]", "goodput [Mbit/s]"]


def _apply_ts_limits(axes, lim):
    axes[2].set_yscale("symlog", linthresh=1)
    if not lim:
        return
    for ax, key in zip(axes, ["cwnd", "rtt", "loss", "goodput"]):
        if lim.get(key):
            ax.set_ylim(*lim[key])
    if lim.get("t"):
        axes[-1].set_xlim(*lim["t"])


def ts_limits(ds):
    return {
        "cwnd": _limits([d["cwnd_seg"] for d in ds]),
        "rtt": _limits([d["srtt_ms"] + d["rttvar_ms"] for d in ds]),
        "loss": _limits([d["d_retrans"] for d in ds]),
        "goodput": _limits([d["goodput_mbps"] for d in ds]),
        "t": _limits([d["t"] for d in ds], pad=0.0),
    }


def plot_timeseries(path, d, alg, title, lim=None):
    fig, axes = plt.subplots(4, 1, figsize=(7.5, 8.2), sharex=True)
    _ts_axes_fill(axes, d, alg)
    for ax, yl in zip(axes, TS_YLABELS):
        ax.set_ylabel(yl)
        ax.legend(loc="upper right", fontsize=7)
    axes[-1].set_xlabel("time since start of transfer [s]")
    _apply_ts_limits(axes, lim)
    fig.suptitle(title, fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


SC_SPECS = [("cwnd_seg", "snd_cwnd [segments]"), ("srtt_ms", "srtt [ms]"),
            ("d_retrans", "retransmitted segments / interval (symlog)")]


def sc_limits(ds):
    lim = {k: _limits([d[k] for d in ds]) for k, _ in SC_SPECS}
    lim["goodput"] = _limits([d["goodput_mbps"] for d in ds])
    return lim


def _scatter(ax, d, key, alg, i=0):
    st = style(alg, i)
    ax.scatter(d[key], d["goodput_mbps"], s=9, marker=st["marker"], color=st["color"],
               alpha=0.45, linewidths=0)
    x, y = d[key], d["goodput_mbps"]
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() > 3 and np.nanstd(x[ok]) > 0 and np.nanstd(y[ok]) > 0:
        r = np.corrcoef(x[ok], y[ok])[0, 1]
        ax.text(0.98, 0.04, "Pearson r = %.2f" % r, transform=ax.transAxes, ha="right",
                fontsize=7, color=INK)


def plot_scatter(path, d, alg, title, lim=None):
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.4), sharey=True)
    for ax, (key, xl) in zip(axes, SC_SPECS):
        _scatter(ax, d, key, alg)
        ax.set_xlabel(xl)
        if key == "d_retrans":
            ax.set_xscale("symlog", linthresh=1)
        if lim and lim.get(key):
            ax.set_xlim(*lim[key])
    axes[0].set_ylabel("goodput [Mbit/s]")
    if lim and lim.get("goodput"):
        axes[0].set_ylim(*lim["goodput"])
    fig.suptitle(title, fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _concat(ds):
    return {k: np.concatenate([d[k] for d in ds]) for k in ds[0]} if ds else None


def plot_goodput_all(path, runs, samples, rows_per_page=6):
    algs = _algorithms(runs)
    dests = _destinations(runs)
    ymax_all = []
    with PdfPages(path) as pdf:
        for p in range(0, len(dests), rows_per_page):
            page = dests[p:p + rows_per_page]
            fig, axes = plt.subplots(len(page), len(algs), figsize=(3.3 * len(algs), 1.75 * len(page) + 0.9),
                                     sharex=True, squeeze=False)
            for ri, dest in enumerate(page):
                rs_dest = [r for r in runs if r["destination"] == dest and r["status"] in USABLE
                           and samples.get(r["run_id"]) is not None]
                lim = _limits([derived(samples[r["run_id"]])["goodput_mbps"] for r in rs_dest])
                for ci, alg in enumerate(algs):
                    ax = axes[ri][ci]
                    st = style(alg, ci)
                    reps = sorted([r for r in rs_dest if r["algorithm"] == alg], key=lambda r: r["rep"])
                    lss = ["-", "--", ":", "-."]
                    for k, r in enumerate(reps):
                        d = derived(samples[r["run_id"]])
                        ax.plot(d["t"], d["goodput_mbps"], color=st["color"], ls=lss[k % 4],
                                lw=0.9, alpha=0.9, label="rep %d" % r["rep"])
                    if not reps:
                        ax.text(0.5, 0.5, "no usable run", transform=ax.transAxes,
                                ha="center", va="center", color=MUTED)
                    if lim:
                        ax.set_ylim(*lim)
                        ymax_all.append(lim[1])
                    if ri == 0:
                        ax.set_title(alg, color=INK)
                    if ci == 0:
                        ax.set_ylabel("%s\nMbit/s" % dest, fontsize=7.5)
                    if reps and ci == len(algs) - 1:
                        ax.legend(fontsize=6.5, loc="upper right")
            for ax in axes[-1]:
                ax.set_xlabel("time [s]")
            fig.suptitle("Goodput over time (Δ tcpi_bytes_acked / Δt), all destinations "
                         "(page %d)" % (p // rows_per_page + 1), fontsize=10, color=INK)
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)


def plot_regimes(path, per_run):
    if not per_run:
        return
    algs = []
    for p in per_run:
        if p["algorithm"] not in algs:
            algs.append(p["algorithm"])
    dests = []
    for p in per_run:
        if p["destination"] not in dests:
            dests.append(p["destination"])
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    width = 0.8 / max(len(algs), 1)
    for ai, alg in enumerate(algs):
        st = style(alg, ai)
        rs = [p for p in per_run if p["algorithm"] == alg]
        x = np.array([dests.index(p["destination"]) for p in rs]) + (ai - (len(algs) - 1) / 2) * width
        kw = dict(marker=st["marker"], color=st["color"], s=22, alpha=0.85, label=alg, linewidths=0)
        axes[0][0].scatter(x, [p["goodput_mean_mbps"] for p in rs], **kw)
        axes[0][1].scatter(x, [p["srtt_mean_ms"] for p in rs], **kw)
        axes[1][0].scatter([p["min_rtt_ms"] for p in rs], [p["goodput_mean_mbps"] for p in rs], **kw)
        axes[1][1].scatter([p["retrans_rate_pct"] for p in rs], [p["goodput_mean_mbps"] for p in rs], **kw)
    for ax in axes[0]:
        ax.set_xticks(range(len(dests)))
        ax.set_xticklabels(dests, rotation=35, ha="right", fontsize=7)
    axes[0][0].set_ylabel("mean goodput per run [Mbit/s]")
    axes[0][0].set_title("Throughput per destination (each dot = one run)")
    axes[0][1].set_ylabel("mean srtt per run [ms]")
    axes[0][1].set_title("RTT per destination (each dot = one run)")
    axes[1][0].set_xlabel("path min_rtt [ms]")
    axes[1][0].set_ylabel("mean goodput [Mbit/s]")
    axes[1][0].set_title("Throughput vs propagation delay")
    axes[1][1].set_xlabel("retransmitted bytes / sent bytes [%]")
    axes[1][1].set_ylabel("mean goodput [Mbit/s]")
    axes[1][1].set_title("Throughput vs loss")
    axes[0][0].legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def make_plots(out_dir, runs, samples, per_run, rep_dest=None, warmup=0.0):
    pdir = os.path.join(out_dir, "plots")
    os.makedirs(pdir, exist_ok=True)
    algs = _algorithms(runs)
    made = []

    p = os.path.join(pdir, "q1_goodput_timeseries.pdf")
    plot_goodput_all(p, runs, samples)
    made.append(p)

    dest, rep = choose_representative(runs, samples, rep_dest)
    if dest is None:
        print("No usable runs: skipping Q2/Q3 plots")
        return made, None
    print("Representative destination: %s (repetition %s)" % (dest, rep))

    # Representative time series = one repetition; scatter = all repetitions.
    ts, sc, actual = {}, {}, {}
    for alg in algs:
        r = _get_run(runs, dest, alg, rep)
        if r is None:
            continue
        ts[alg] = (derived(samples[r["run_id"]]), r)
        reps = [x for x in runs if x["destination"] == dest and x["algorithm"] == alg
                and x["status"] in USABLE and samples.get(x["run_id"]) is not None]
        sc[alg] = _concat([_warm(derived(samples[x["run_id"]]), warmup) for x in reps])
        actual[alg] = r.get("actual_cc")
    if not ts:
        return made, dest

    tlim = ts_limits([v[0] for v in ts.values()])
    slim = sc_limits(list(sc.values()))

    # Q2 (b) and Q3 (a): the same figures for every algorithm, consistent axes.
    for alg, (d, r) in ts.items():
        name = "q2_timeseries_%s.pdf" % alg
        plot_timeseries(os.path.join(pdir, name), d, alg,
                        "%s — %s (actual: %s), rep %s" % (dest, alg, actual[alg], r["rep"]), tlim)
        made.append(os.path.join(pdir, name))
        name = "q2_scatter_%s.pdf" % alg
        plot_scatter(os.path.join(pdir, name), sc[alg], alg,
                     "%s — %s (actual: %s), all repetitions" % (dest, alg, actual[alg]), slim)
        made.append(os.path.join(pdir, name))

    # Side-by-side comparison, shared axes.
    n = len(ts)
    fig, axes = plt.subplots(4, n, figsize=(3.6 * n, 8.5), sharex=True, squeeze=False)
    for ci, (alg, (d, r)) in enumerate(ts.items()):
        col = [axes[ri][ci] for ri in range(4)]
        _ts_axes_fill(col, d, alg, ci)
        _apply_ts_limits(col, tlim)
        col[0].set_title("%s (actual: %s)" % (alg, actual[alg]), color=INK)
        col[-1].set_xlabel("time [s]")
        if ci == 0:
            for ax, yl in zip(col, TS_YLABELS):
                ax.set_ylabel(yl)
        if ci == n - 1:
            for ax in col:
                ax.legend(fontsize=6.5, loc="upper right")
    fig.suptitle("Time series, %s, rep %s (same axes for all algorithms)" % (dest, rep),
                 fontsize=10, color=INK)
    fig.tight_layout()
    p = os.path.join(pdir, "q3_timeseries_compare.pdf")
    fig.savefig(p)
    plt.close(fig)
    made.append(p)

    # Overlay version.
    fig, axes = plt.subplots(4, 1, figsize=(7.5, 8.2), sharex=True)
    for ci, (alg, (d, r)) in enumerate(ts.items()):
        _ts_axes_fill(axes, d, alg, ci, label=alg, show_band=False)
    for ax, yl in zip(axes, TS_YLABELS):
        ax.set_ylabel(yl)
    axes[0].legend(loc="upper right", fontsize=7)
    axes[-1].set_xlabel("time since start of transfer [s]")
    _apply_ts_limits(axes, tlim)
    fig.suptitle("Algorithms overlaid, %s, rep %s" % (dest, rep), fontsize=10, color=INK)
    fig.tight_layout()
    p = os.path.join(pdir, "q3_timeseries_overlay.pdf")
    fig.savefig(p)
    plt.close(fig)
    made.append(p)

    # Scatter comparison grid: rows = metric, columns = algorithm.
    fig, axes = plt.subplots(3, n, figsize=(3.6 * n, 8.2), squeeze=False)
    for ci, alg in enumerate(sc):
        for ri, (key, xl) in enumerate(SC_SPECS):
            ax = axes[ri][ci]
            _scatter(ax, sc[alg], key, alg, ci)
            if key == "d_retrans":
                ax.set_xscale("symlog", linthresh=1)
            if slim.get(key):
                ax.set_xlim(*slim[key])
            if slim.get("goodput"):
                ax.set_ylim(*slim["goodput"])
            ax.set_xlabel(xl)
            if ci == 0:
                ax.set_ylabel("goodput [Mbit/s]")
        axes[0][ci].set_title("%s (actual: %s)" % (alg, actual[alg]), color=INK)
    fig.suptitle("Goodput relationships, %s, all repetitions (same axes)" % dest,
                 fontsize=10, color=INK)
    fig.tight_layout()
    p = os.path.join(pdir, "q3_scatter_compare.pdf")
    fig.savefig(p)
    plt.close(fig)
    made.append(p)

    p = os.path.join(pdir, "q3_regimes.pdf")
    plot_regimes(p, per_run)
    made.append(p)
    return made, dest


def analyze(out_dir, rep_dest=None, warmup=0.0):
    runs = load_runs(out_dir)
    if not runs:
        print("No runs found in", out_dir)
        return
    samples = {r["run_id"]: load_samples(out_dir, r["run_id"]) for r in runs}
    per_run, _, _ = build_tables(out_dir, runs, samples, warmup)
    made, dest = make_plots(out_dir, runs, samples, per_run, rep_dest, warmup=warmup)
    meta_path = os.path.join(out_dir, "metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        meta["representative_destination"] = dest
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
    print("Tables: %s" % os.path.join(out_dir, "tables"))
    for p in made:
        print("Plot:  ", p)
