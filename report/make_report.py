#!/usr/bin/env python3
"""Build the Assignment 3 report PDF from a results directory.

    python3 report/make_report.py                       # newest results/<run>
    python3 report/make_report.py results/20261018-133538
    python3 report/make_report.py --tex-only            # write generated/*.tex, skip pdflatex

Reads   results/<run>/{metadata.json,test_order.csv,destinations.csv,tables/*.csv}
        and the source files (to find function line ranges for the GitHub links).
Writes  report/generated/{data.tex,aggregate.tex,per_dest_goodput.tex,per_dest_rtt.tex,
        order.tex,destinations.tex}
        report/report.pdf  (pdflatex run twice on report/report.tex)

Every number in the report comes from these files, so re-running the
experiments and then this script regenerates the whole PDF.
"""
import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(HERE, "generated")
REPO_URL = "https://github.com/philwisniewski/cs-422-assignment-3"
REPO_REF = "main"

ALGS = ["cubic", "reno", "bbr", "gentle_reno"]
ALG_LABEL = {"cubic": "CUBIC", "reno": "Reno", "bbr": "BBR", "gentle_reno": "Gentle Reno"}

# (macro name, file, kind, symbol): line ranges are looked up in the source so
# the links stay correct when the code moves.
CODE_LINKS = [
    ("grParams", "bpf/gentle_reno.bpf.c", "block", "#define BETA_NUM"),
    ("grState", "bpf/gentle_reno.bpf.c", "c_struct", "struct gentle_reno {"),
    ("grInit", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_init"),
    ("grSsthresh", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_ssthresh"),
    ("grCongAvoid", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_cong_avoid"),
    ("grPktsAcked", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_pkts_acked"),
    ("grSetState", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_set_state"),
    ("grUndo", "bpf/gentle_reno.bpf.c", "bpf_prog", "gr_undo_cwnd"),
    ("grOps", "bpf/gentle_reno.bpf.c", "c_struct", "struct tcp_congestion_ops gentle_reno = {"),
    ("pyConnect", "iperf3_client.py", "py_def", "_connect"),
    ("pySample", "iperf3_client.py", "py_def", "_sample"),
    ("pyRun", "iperf3_client.py", "py_def", "run"),
    ("pySetCC", "tcp_info.py", "py_def", "set_congestion_control"),
    ("pyGetCC", "tcp_info.py", "py_def", "get_congestion_control"),
    ("pyPreflight", "run_experiments.py", "py_def", "preflight"),
    ("pyBpfSnapshot", "run_experiments.py", "py_def", "bpf_snapshot"),
    ("pySelect", "run_experiments.py", "py_def", "select_destinations"),
    ("pyRunExperiments", "run_experiments.py", "py_def", "run_experiments"),
    ("pyRunStats", "analysis.py", "py_def", "run_stats"),
    ("pyBuildTables", "analysis.py", "py_def", "build_tables"),
    ("pyChooseRep", "analysis.py", "py_def", "choose_representative"),
    ("pyPlotTs", "analysis.py", "py_def", "plot_timeseries"),
    ("pyPlotScatter", "analysis.py", "py_def", "plot_scatter"),
    ("pyPlotRegimes", "analysis.py", "py_def", "plot_regimes"),
    ("pyMakePlots", "analysis.py", "py_def", "make_plots"),
    ("pyReport", "report/make_report.py", "py_def", "main"),
]


# ------------------------------------------------------------------ helpers
def tex(s):
    """Escape a plain string for LaTeX."""
    s = str(s)
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                 ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                 ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")):
        s = s.replace(a, b)
    return s


def dest_tex(d):
    # allow line breaks after dots in long host names
    return r"\texttt{%s}" % tex(d).replace(".", r".\allowbreak{}")


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return math.nan
    return x


def fmt(v, nd=1):
    x = num(v)
    return "--" if math.isnan(x) else "%.*f" % (nd, x)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def find_lines(path, kind, symbol):
    with open(os.path.join(ROOT, path)) as f:
        lines = f.read().split("\n")
    if kind == "bpf_prog":
        for i, l in enumerate(lines):
            if "BPF_PROG(%s," % symbol in l or "BPF_PROG(%s)" % symbol in l:
                start = i if not lines[i - 1].startswith("SEC(") else i - 1
                for j in range(i, len(lines)):
                    if lines[j].startswith("}"):
                        return start + 1, j + 1
    elif kind == "c_struct":
        for i, l in enumerate(lines):
            if l.startswith(symbol):
                for j in range(i, len(lines)):
                    if lines[j].startswith("}"):
                        return i + 1, j + 1
    elif kind == "block":
        for i, l in enumerate(lines):
            if l.startswith(symbol):
                j = i
                while j + 1 < len(lines) and lines[j + 1].startswith("#define"):
                    j += 1
                # include the following #defines separated by one blank line
                while (j + 2 < len(lines) and lines[j + 1].strip() == ""
                       and lines[j + 2].startswith("#define")):
                    j += 2
                    while j + 1 < len(lines) and lines[j + 1].startswith("#define"):
                        j += 1
                return i + 1, j + 1
    elif kind == "py_def":
        pat = re.compile(r"^(\s*)def %s\(" % re.escape(symbol))
        for i, l in enumerate(lines):
            m = pat.match(l)
            if not m:
                continue
            ind = len(m.group(1))
            end = i
            for j in range(i + 1, len(lines)):
                s = lines[j]
                if s.strip() == "":
                    continue
                if len(s) - len(s.lstrip()) <= ind:
                    break
                end = j
            return i + 1, end + 1
    raise SystemExit("make_report: cannot find %s %r in %s" % (kind, symbol, path))


def latest_run():
    rdir = os.path.join(ROOT, "results")
    runs = sorted(d for d in os.listdir(rdir)
                  if os.path.exists(os.path.join(rdir, d, "tables", "aggregate_by_algorithm.csv")))
    if not runs:
        raise SystemExit("make_report: no results/<run>/tables/aggregate_by_algorithm.csv found")
    return os.path.join(rdir, runs[-1])


def define(out, name, value):
    out.append(r"\expandafter\def\csname %s\endcsname{%s}" % (name, value))


# ------------------------------------------------------------------ writers
def write_data(run_dir, meta, agg, per_run, order, out_path):
    out = ["% generated by report/make_report.py -- do not edit"]
    rel_plots = os.path.relpath(os.path.join(run_dir, "plots"), HERE).replace(os.sep, "/")
    out.append(r"\newcommand{\plotdir}{%s/}" % rel_plots)
    out.append(r"\newcommand{\rundir}{%s}" % tex(os.path.relpath(run_dir, ROOT)))
    out.append(r"\newcommand{\repourl}{%s}" % REPO_URL)
    out.append(r"\newcommand{\reporef}{%s}" % REPO_REF)

    host = meta.get("host", {})
    args = meta.get("args", {})
    for k, v in [("kernel", host.get("kernel")), ("uname", host.get("uname")),
                 ("python", host.get("python")), ("availcc", host.get("available_cc")),
                 ("defaultcc", host.get("default_cc")), ("command", meta.get("command")),
                 ("created", meta.get("created")), ("finished", meta.get("finished")),
                 ("repdest", meta.get("representative_destination")),
                 ("seed", meta.get("seed")), ("numdests", args.get("num_dests")),
                 ("duration", fmt(args.get("duration"), 0)), ("interval", args.get("interval")),
                 ("reps", args.get("reps")), ("pause", fmt(args.get("pause"), 0)),
                 ("blksize", args.get("blksize")),
                 ("serversparsed", meta.get("servers_parsed")),
                 ("selattempts", meta.get("selection_attempts"))]:
        define(out, "meta@" + k, tex(v))
    for alg, cc in (host.get("cc_check") or {}).items():
        define(out, "cccheck@" + alg, tex(cc))
    for k, v in ((meta.get("gentle_reno_bpf_after") or {}).get("counters") or {}).items():
        define(out, "bpf@" + k, v)

    # test order / failures
    n_total = len(order)
    n_ok = sum(1 for o in order if o["status"] == "ok")
    failed = [o for o in order if o["status"] != "ok"]
    define(out, "order@total", n_total)
    define(out, "order@ok", n_ok)
    define(out, "order@failed", len(failed))
    define(out, "order@faileddests",
           ", ".join(sorted({dest_tex(o["destination"]) for o in failed})) or "none")

    # aggregate stats
    for row in agg:
        for k, v in row.items():
            if k in ("algorithm", "algorithm_actual"):
                define(out, "agg@%s@%s" % (row["algorithm"], k), tex(v))
            else:
                define(out, "agg@%s@%s" % (row["algorithm"], k), fmt(v, 2))

    # cwnd relative to the path: per destination, the largest BDP estimate any
    # algorithm reached there (max delivery rate x min RTT) is the path BDP.
    path_bdp = {}
    for r in per_run:
        b = num(r["bdp_est_kB"])
        if not math.isnan(b):
            path_bdp[r["destination"]] = max(path_bdp.get(r["destination"], 0.0), b)
    for alg in ALGS:
        rs = [r for r in per_run if r["algorithm"] == alg]
        cw = [num(r["cwnd_mean_seg"]) for r in rs]
        cw = [x for x in cw if not math.isnan(x)]
        rel = [num(r["cwnd_mean_kB"]) / path_bdp[r["destination"]] for r in rs
               if path_bdp.get(r["destination"]) and not math.isnan(num(r["cwnd_mean_kB"]))]
        define(out, "cwnd@%s@seg" % alg, fmt(sum(cw) / len(cw), 0) if cw else "--")
        define(out, "cwnd@%s@pathbdp" % alg, fmt(100 * sum(rel) / len(rel), 0) if rel else "--")
    out.append(r"\newcommand{\stat}[2]{\csname #1@#2\endcsname}")
    out.append(r"\newcommand{\agg}[2]{\csname agg@#1@#2\endcsname}")
    out.append(r"\newcommand{\meta}[1]{\csname meta@#1\endcsname}")
    out.append(r"\newcommand{\bpfstat}[1]{\csname bpf@#1\endcsname}")

    for name, path, kind, sym in CODE_LINKS:
        a, b = find_lines(path, kind, sym)
        url = "%s/blob/%s/%s#L%d-L%d" % (REPO_URL, REPO_REF, path, a, b)
        label = r"\texttt{%s}" % tex("%s:%d-%d" % (path, a, b))
        out.append(r"\newcommand{\%s}{\href{%s}{%s}}" % (name, url.replace("#", r"\#"), label))
    for path in ["bpf/gentle_reno.bpf.c", "bpf/Makefile", "run_all.sh", "run_experiments.py",
                 "iperf3_client.py", "tcp_info.py", "analysis.py", "servers.py",
                 "tools/local_testbed.sh", "docs/a2_pseudocode.txt", "docs/implementation.md",
                 "report/make_report.py", "report/report.tex"]:
        key = re.sub(r"[^A-Za-z]", "", path)
        url = "%s/blob/%s/%s" % (REPO_URL, REPO_REF, path)
        out.append(r"\expandafter\def\csname file@%s\endcsname{\href{%s}{\texttt{%s}}}"
                   % (key, url, tex(path)))
    out.append(r"\newcommand{\file}[1]{\csname file@#1\endcsname}")

    with open(out_path, "w") as f:
        f.write("\n".join(out) + "\n")


def write_aggregate(agg, out_path):
    by = {r["algorithm"]: r for r in agg}
    rows = [
        ("Runs / interval samples", lambda r: "%s / %s" % (r["runs"], r["samples"])),
        ("Goodput mean [Mbit/s]", lambda r: fmt(r["goodput_mean_mbps"])),
        ("Goodput median [Mbit/s]", lambda r: fmt(r["goodput_median_mbps"])),
        ("Goodput p5 [Mbit/s]", lambda r: fmt(r["goodput_p5_mbps"], 2)),
        ("Goodput p95 [Mbit/s]", lambda r: fmt(r["goodput_p95_mbps"])),
        ("srtt mean [ms]", lambda r: fmt(r["srtt_mean_ms"])),
        ("srtt median [ms]", lambda r: fmt(r["srtt_median_ms"])),
        ("srtt p95 [ms]", lambda r: fmt(r["srtt_p95_ms"])),
        ("RTT inflation (srtt $-$ min\\_rtt) [ms]", lambda r: fmt(r["rtt_inflation_mean_ms"], 2)),
        ("Retransmitted bytes [\\%]", lambda r: fmt(r["retrans_rate_mean_pct"], 2)),
        ("Retransmitted segments / run", lambda r: fmt(r["retrans_per_run_mean"], 0)),
        ("RTOs / run", lambda r: fmt(r["rto_per_run_mean"], 2)),
    ]
    algs = [a for a in ALGS if a in by]
    lines = [r"\begin{tabular}{l" + "r" * len(algs) + "}", r"\toprule",
             " & ".join(["Statistic"] + [r"\textbf{%s}" % ALG_LABEL[a] for a in algs]) + r" \\",
             r"\midrule"]
    for label, f in rows:
        lines.append(" & ".join([label] + [f(by[a]) for a in algs]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_per_dest(comp, metric, out_path):
    """metric: 'goodput' (mean +- std of per-run mean goodput) or 'srtt'."""
    algs = [a for a in ALGS if any(("%s_runs" % a) in r for r in comp)]
    lines = [r"\begin{tabular}{>{\raggedright\arraybackslash}p{5.4cm}" + "r" * len(algs) + "}",
             r"\toprule",
             " & ".join(["Destination"] + [r"\textbf{%s}" % ALG_LABEL[a] for a in algs]) + r" \\",
             r"\midrule"]
    for r in comp:
        vals = []
        best_key = "best_goodput" if metric == "goodput" else "lowest_rtt"
        for a in algs:
            m = r.get("%s_%s" % (a, "goodput_mbps" if metric == "goodput" else "srtt_ms"))
            s = r.get("%s_%s" % (a, "goodput_std" if metric == "goodput" else "srtt_std"))
            n = r.get("%s_runs" % a)
            cell = "%s $\\pm$ %s" % (fmt(m), fmt(s))
            if n and n != "5" and r["destination"] != "ALL":
                cell += r"$^{(%s)}$" % n
            if r.get(best_key) == a:
                cell = r"\textbf{%s}" % cell
            vals.append(cell)
        d = r"\textit{All destinations}" if r["destination"] == "ALL" else dest_tex(r["destination"])
        if r["destination"] == "ALL":
            lines.append(r"\midrule")
        lines.append(" & ".join([d] + vals) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_destinations(dests, out_path):
    lines = [r"\begin{tabular}{rlllr}", r"\toprule",
             r"\# & Destination & Location & Provider & Gbit/s \\", r"\midrule"]
    for d in dests:
        lines.append(r"%s & %s & %s & %s & %s \\" % (
            d["dest_index"], dest_tex("%s:%s" % (d["host"], d["port"])),
            tex("%s, %s" % (d["site"], d["country"])), tex(d["provider"]), tex(d["gbps"])))
    lines += [r"\bottomrule", r"\end{tabular}"]
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_order(order, out_path):
    """Compact view of the test order: one line per (rep, destination)."""
    groups = []
    for o in order:
        key = (o["rep"], o["dest_index"], o["destination"])
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        a = ALG_LABEL.get(o["algorithm"], o["algorithm"])
        if o["status"] != "ok":
            a = r"\sout{%s}" % a
        groups[-1][1].append(a)
    lines = [r"\begin{longtable}{rrll}", r"\toprule",
             r"Rep & Dest & Destination & Algorithm order \\", r"\midrule", r"\endhead"]
    for (rep, di, dest), algs in groups:
        lines.append(r"%s & %s & %s & %s \\" % (rep, di, dest_tex(dest), r" $\to$ ".join(algs)))
    lines += [r"\bottomrule", r"\end{longtable}"]
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("run_dir", nargs="?", help="results/<run> (default: newest)")
    ap.add_argument("--tex-only", action="store_true", help="only write report/generated/*.tex")
    args = ap.parse_args(argv)

    run_dir = os.path.abspath(args.run_dir) if args.run_dir else latest_run()
    tdir = os.path.join(run_dir, "tables")
    with open(os.path.join(run_dir, "metadata.json")) as f:
        meta = json.load(f)
    agg = read_csv(os.path.join(tdir, "aggregate_by_algorithm.csv"))
    comp = read_csv(os.path.join(tdir, "algorithm_comparison.csv"))
    per_run = read_csv(os.path.join(tdir, "run_summary.csv"))
    order = read_csv(os.path.join(run_dir, "test_order.csv"))
    dests = read_csv(os.path.join(run_dir, "destinations.csv"))

    os.makedirs(GEN, exist_ok=True)
    write_data(run_dir, meta, agg, per_run, order, os.path.join(GEN, "data.tex"))
    write_aggregate(agg, os.path.join(GEN, "aggregate.tex"))
    write_per_dest(comp, "goodput", os.path.join(GEN, "per_dest_goodput.tex"))
    write_per_dest(comp, "srtt", os.path.join(GEN, "per_dest_rtt.tex"))
    write_destinations(dests, os.path.join(GEN, "destinations.tex"))
    write_order(order, os.path.join(GEN, "order.tex"))
    print("make_report: wrote %s/*.tex from %s" % (os.path.relpath(GEN, ROOT),
                                                    os.path.relpath(run_dir, ROOT)))
    if args.tex_only:
        return 0

    latex = shutil.which("pdflatex")
    if not latex:
        raise SystemExit("make_report: pdflatex not found (install TeX Live / MacTeX / BasicTeX)")
    for _ in range(2):  # second pass resolves references and the table of contents
        p = subprocess.run([latex, "-interaction=nonstopmode", "-halt-on-error", "report.tex"],
                           cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           errors="replace")
        if p.returncode != 0:
            sys.stdout.write(p.stdout[-4000:])
            raise SystemExit("make_report: pdflatex failed (full log: report/report.log)")
    print("make_report: wrote report/report.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
