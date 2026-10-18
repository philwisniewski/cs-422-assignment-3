#!/usr/bin/env python3
"""
CS 422 Assignment 3 (Option 1) — single entry point.

Same pipeline as Assignment 2, now comparing four algorithms: CUBIC, Reno, BBR and
gentle_reno, our eBPF implementation (bpf/gentle_reno.bpf.c), which must be
registered first:  sudo make -C bpf load

Pipeline:
  1. preflight   record kernel/CC info, check every requested CC can be selected
  2. selection   pick n random servers from the list; probe each with a short
                 iperf3 test; on failure log it and try a replacement (bounded)
  3. experiments for rep in 1..R, for each destination, run every algorithm
                 (order rotated per rep and destination) for `duration` seconds,
                 sampling TCP_INFO every `interval` seconds on the data socket
  4. analysis    summary tables (CSV + Markdown) and PDF plots

Example:
  sudo python3 run_experiments.py --servers servers.csv -n 10 -t 10 -i 0.2 -r 5
  python3 run_experiments.py --plot-only results/20261005-1100   # re-plot
"""

import argparse
import csv
import json
import os
import platform
import random
import glob
import shutil
import socket
import subprocess
import sys
import time

from iperf3_client import Iperf3Client, RETRYABLE
from servers import load_servers, is_ipv6_literal
from tcp_info import FORMAT_DESCRIPTION, set_congestion_control, get_congestion_control
import analysis

SAMPLE_COLUMNS = [
    "run_id", "destination", "algorithm", "algorithm_actual", "rep", "sample_idx",
    "t_s", "timestamp_unix", "dt_s",
    "goodput_bps", "bytes_acked_B", "d_bytes_acked_B",
    "snd_cwnd_seg", "snd_mss_B", "cwnd_B", "snd_ssthresh_seg",
    "srtt_us", "rttvar_us", "min_rtt_us", "rto_us",
    "total_retrans_seg", "d_retrans_seg", "bytes_retrans_B", "d_bytes_retrans_B",
    "lost_seg", "retrans_inflight_seg", "unacked_seg", "sacked_seg",
    "total_rto", "d_total_rto", "ca_state",
    "bytes_sent_B", "d_bytes_sent_B", "app_bytes_sent_B", "notsent_bytes_B",
    "pacing_rate_Bps", "delivery_rate_Bps", "delivery_rate_app_limited", "delivered_seg",
    "busy_time_us", "rwnd_limited_us", "sndbuf_limited_us", "snd_wnd_B", "tcp_info_len_B",
]


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def read_sysctl(name):
    try:
        with open("/proc/sys/" + name.replace(".", "/")) as f:
            return f.read().strip()
    except OSError:
        return None


def _bpftool():
    for cand in [shutil.which("bpftool")] + sorted(glob.glob("/usr/lib/linux-tools-*/bpftool")):
        if cand:
            try:
                subprocess.run([cand, "version"], capture_output=True, check=True, timeout=5)
                return cand
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def bpf_snapshot():
    """Registration info + counters of the gentle_reno struct_ops program
    (best effort; needs root and bpftool). Saved with the results as evidence
    that the experiments ran on our eBPF implementation."""
    tool = _bpftool()
    if not tool:
        return {"error": "bpftool not found"}
    out = {}
    try:
        r = subprocess.run([tool, "-j", "struct_ops", "show", "name", "gentle_reno"],
                           capture_output=True, text=True, timeout=10)
        out["struct_ops"] = json.loads(r.stdout) if r.stdout.strip() else r.stderr.strip()
        maps = json.loads(subprocess.run([tool, "-j", "map", "show"], capture_output=True,
                                         text=True, timeout=10).stdout or "[]")
        bss = [m for m in maps if m.get("name", "").startswith("gentle_r") and "bss" in m.get("name", "")]
        if bss:
            r = subprocess.run([tool, "-j", "map", "dump", "id", str(bss[-1]["id"])],
                               capture_output=True, text=True, timeout=10)
            dump = json.loads(r.stdout)
            out["counters"] = {k: v for d in dump[0]["formatted"]["value"][".bss"] for k, v in d.items()} \
                if dump and "formatted" in dump[0] else dump
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def preflight(algorithms):
    info = {
        "kernel": platform.release(),
        "uname": " ".join(platform.uname()),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "default_cc": read_sysctl("net.ipv4.tcp_congestion_control"),
        "available_cc": read_sysctl("net.ipv4.tcp_available_congestion_control"),
        "allowed_cc": read_sysctl("net.ipv4.tcp_allowed_congestion_control"),
        "in_docker": os.path.exists("/.dockerenv"),
        "cc_check": {},
    }
    for alg in algorithms:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            set_congestion_control(s, alg)
            info["cc_check"][alg] = get_congestion_control(s)
        except OSError as e:
            info["cc_check"][alg] = "UNAVAILABLE: %s" % e
        finally:
            s.close()
    return info


def run_on_server(server, port_order, args, congestion, duration, tag):
    """Run one test on `server`, trying other ports of its range if a port is
    busy/refused. Returns (result, port)."""
    result, port = None, None
    for port in port_order[:max(1, args.ports_per_server)]:
        log("  %s %s:%d cc=%s t=%.0fs" % (tag, server["host"], port, congestion or "default", duration))
        client = Iperf3Client(server["host"], port, duration=duration, interval=args.interval,
                              congestion=congestion, connect_timeout=args.connect_timeout,
                              ctrl_timeout=args.ctrl_timeout, stall_timeout=args.stall_timeout,
                              blksize=args.blksize, ipv4_only=args.ipv4_only, log=log)
        result = client.run()
        if result["status"] in analysis.USABLE:
            return result, port
        log("    -> %s (%s/%s): %s" % (result["status"], result["error_kind"],
                                       result["error_phase"], result["error_msg"]))
        if result["error_kind"] not in ("busy", "connect_refused"):
            break
        time.sleep(1.0)
    return result, port


def select_destinations(servers, args, rng, out_dir):
    cands = list(servers)
    rng.shuffle(cands)
    selected, attempts, logrows = [], 0, []
    max_attempts = args.max_attempts or 3 * args.num_dests
    log("Selecting %d destinations from %d candidates (max %d attempts)"
        % (args.num_dests, len(cands), max_attempts))
    for srv in cands:
        if len(selected) >= args.num_dests or attempts >= max_attempts:
            break
        if any(s["host"].lower() == srv["host"].lower() for s in selected):
            continue  # same machine listed twice (other port range) -> not a new destination
        attempts += 1
        ports = list(srv["ports"])
        rng.shuffle(ports)
        res, port = run_on_server(srv, ports, args, None, args.probe_duration,
                                  "probe %d/%d" % (attempts, max_attempts))
        ok = res["status"] in analysis.USABLE
        logrows.append({
            "attempt": attempts, "host": srv["host"], "port": port, "ports_listed": len(srv["ports"]),
            "country": srv["country"], "site": srv["site"], "provider": srv["provider"],
            "outcome": "selected" if ok else "rejected", "status": res["status"],
            "error_kind": res["error_kind"], "error_phase": res["error_phase"],
            "error_msg": res["error_msg"],
            "probe_goodput_mbps": (round(res["samples"][-1]["bytes_acked_B"] * 8 / 1e6 /
                                   max(res["duration_actual_s"], 1e-9), 2)
                                   if ok and res["samples"] else None),
        })
        if ok:
            # Probe ran on whichever port worked; keep that port first.
            entry = dict(srv)
            entry["port"] = port
            entry["port_order"] = [port] + [p for p in srv["ports"] if p != port]
            selected.append(entry)
            log("    selected #%d: %s:%d" % (len(selected), srv["host"], port))
        time.sleep(args.pause)

    path = os.path.join(out_dir, "selection_log.csv")
    with open(path, "w", newline="") as f:
        cols = ["attempt", "host", "port", "ports_listed", "country", "site", "provider",
                "outcome", "status", "error_kind", "error_phase", "error_msg", "probe_goodput_mbps"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(logrows)
    shortfall = args.num_dests - len(selected)
    if shortfall > 0:
        log("WARNING: shortfall of %d destination(s): only %d/%d selected after %d attempts"
            % (shortfall, len(selected), args.num_dests, attempts))
    return selected, attempts, shortfall


def save_samples(out_dir, run_id, run, rows):
    path = os.path.join(out_dir, "samples", run_id + ".csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SAMPLE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r.update(run_id=run_id, destination=run["destination"], algorithm=run["algorithm"],
                     algorithm_actual=run["actual_cc"], rep=run["rep"])
            w.writerow(r)


def run_experiments(selected, args, out_dir):
    runs_path = os.path.join(out_dir, "runs.jsonl")
    order_path = os.path.join(out_dir, "test_order.csv")
    order_f = open(order_path, "w", newline="")
    order_w = csv.writer(order_f)
    order_w.writerow(["seq", "rep", "dest_index", "destination", "algorithm", "attempts",
                      "status", "start_unix"])
    algs = args.algorithms
    seq = 0
    total = args.reps * len(selected) * len(algs)
    try:
        for rep in range(1, args.reps + 1):
            for di, dest in enumerate(selected):
                k = (rep - 1 + di) % len(algs)       # rotate algorithm order
                order = algs[k:] + algs[:k]
                for alg in order:
                    seq += 1
                    label = "%s:%d" % (dest["host"], dest["port"])
                    run_id = "r%02d_d%02d_%s" % (rep, di + 1, alg)
                    res, attempt = None, 0
                    for attempt in range(1, args.run_retries + 2):
                        res, port = run_on_server(
                            dest, dest["port_order"], args, alg, args.duration,
                            "[%d/%d] rep %d dest %d %s (try %d)" % (seq, total, rep, di + 1, alg, attempt))
                        if res["status"] in analysis.USABLE or not RETRYABLE.get(res["error_kind"], False):
                            break
                        backoff = args.retry_backoff * attempt + random.uniform(0, 3)
                        log("    retrying in %.0fs" % backoff)
                        time.sleep(backoff)
                    if res["status"] in analysis.USABLE and res["actual_cc"] != alg:
                        log("    WARNING: requested %s but kernel used %s" % (alg, res["actual_cc"]))
                    samples = res.pop("samples")
                    record = dict(res)
                    record.update(run_id=run_id, rep=rep, dest_index=di + 1, destination=label,
                                  algorithm=alg, attempts=attempt, seq=seq,
                                  n_samples=len(samples), actual_cc=res["actual_cc"],
                                  country=dest["country"], site=dest["site"],
                                  provider=dest["provider"])
                    save_samples(out_dir, run_id, record, samples)
                    with open(runs_path, "a") as f:
                        f.write(json.dumps(record) + "\n")
                    order_w.writerow([seq, rep, di + 1, label, alg, attempt, res["status"],
                                      round(res["start_unix"], 3)])
                    order_f.flush()
                    if res["status"] in analysis.USABLE and samples:
                        last = samples[-1]
                        log("    done: %s, %.1f Mbit/s avg, %s retrans, srtt %.1f ms"
                            % (res["status"],
                               last["bytes_acked_B"] * 8 / 1e6 / max(last["t_s"], 1e-9),
                               last["total_retrans_seg"], (last["srtt_us"] or 0) / 1e3))
                    time.sleep(args.pause)
    except KeyboardInterrupt:
        log("Interrupted — analysing the runs completed so far")
    finally:
        order_f.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="iperf3-compatible TCP sender + TCP_INFO tracing + CC comparison incl. "
                    "the gentle_reno eBPF algorithm (CS 422 Assignment 3)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--servers", help="server list CSV from iperf3serverlist.net "
                   "(columns IP/HOST, PORT, GB/S, CONTINENT, COUNTRY, SITE, PROVIDER)")
    p.add_argument("-n", "--num-dests", type=int, default=10, help="number of random destinations")
    p.add_argument("-t", "--duration", type=float, default=10.0, help="transfer duration per run [s]")
    p.add_argument("-i", "--interval", type=float, default=0.2, help="sampling interval [s]")
    p.add_argument("-r", "--reps", type=int, default=5, help="repetitions per destination x algorithm")
    p.add_argument("-a", "--algorithms", default="cubic,reno,bbr,gentle_reno",
                   help="comma-separated congestion control algorithms")
    p.add_argument("-o", "--out", default=None, help="output directory (default results/<timestamp>)")
    p.add_argument("--seed", type=int, default=None, help="random seed for destination selection")
    p.add_argument("--continent", default=None, help="only use servers whose CONTINENT contains this")
    p.add_argument("--country", default=None, help="only use servers whose COUNTRY contains this")
    p.add_argument("--ipv4-only", action="store_true", help="skip IPv6 addresses")
    p.add_argument("--max-attempts", type=int, default=None,
                   help="max servers tried during selection (default 3*n)")
    p.add_argument("--probe-duration", type=float, default=3.0,
                   help="length of the validation test during selection [s]")
    p.add_argument("--ports-per-server", type=int, default=3,
                   help="ports of a server's range to try when one is busy/refused")
    p.add_argument("--run-retries", type=int, default=2,
                   help="extra attempts for a run that fails with a retryable error")
    p.add_argument("--retry-backoff", type=float, default=10.0, help="base backoff between retries [s]")
    p.add_argument("--pause", type=float, default=2.0, help="pause between consecutive tests [s]")
    p.add_argument("--connect-timeout", type=float, default=5.0, help="TCP connect timeout [s]")
    p.add_argument("--ctrl-timeout", type=float, default=15.0, help="control-channel reply timeout [s]")
    p.add_argument("--stall-timeout", type=float, default=15.0,
                   help="abort if no bytes are acknowledged for this long [s]")
    p.add_argument("--blksize", type=int, default=128 * 1024, help="application write size [B]")
    p.add_argument("--rep-dest", default=None, help="force the representative destination (host or host:port)")
    p.add_argument("--warmup", type=float, default=0.0,
                   help="exclude the first N seconds of each run from summary statistics/scatter plots")
    p.add_argument("--plot-only", metavar="DIR", default=None,
                   help="skip measurements; regenerate tables and plots for DIR")
    args = p.parse_args(argv)
    args.algorithms = [a.strip().lower() for a in args.algorithms.split(",") if a.strip()]
    if not args.plot_only and not args.servers:
        p.error("--servers is required (unless --plot-only)")
    if args.interval <= 0 or args.duration <= 0:
        p.error("duration and interval must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.plot_only:
        analysis.analyze(args.plot_only, args.rep_dest, args.warmup)
        return 0

    out_dir = args.out or os.path.join("results", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(out_dir, "samples"), exist_ok=True)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 31)
    rng = random.Random(seed)

    pre = preflight(args.algorithms)
    log("Kernel %s | default CC %s | available: %s | allowed: %s"
        % (pre["kernel"], pre["default_cc"], pre["available_cc"], pre["allowed_cc"]))
    bad = {a: v for a, v in pre["cc_check"].items() if v.startswith("UNAVAILABLE")}
    if bad:
        for a, v in bad.items():
            log("ERROR: congestion control %r %s" % (a, v))
        if "gentle_reno" in bad:
            log("Build and register gentle_reno first:  sudo make -C bpf load   (see README)")
        log("For bbr: `sudo modprobe tcp_bbr`. Non-root users can only select algorithms in "
            "net.ipv4.tcp_allowed_congestion_control, so run as root / with CAP_NET_ADMIN. Aborting.")
        return 2

    servers = load_servers(args.servers)
    if args.continent:
        servers = [s for s in servers if args.continent.lower() in s["continent"].lower()]
    if args.country:
        servers = [s for s in servers if args.country.lower() in s["country"].lower()]
    if args.ipv4_only:
        servers = [s for s in servers if not is_ipv6_literal(s["host"])]
    if not servers:
        log("ERROR: no servers parsed from %s (after filters)" % args.servers)
        return 2

    meta = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "gentle_reno_bpf_before": bpf_snapshot() if "gentle_reno" in args.algorithms else None,
        "command": " ".join([os.path.basename(sys.executable)] + sys.argv),
        "args": vars(args), "seed": seed, "host": pre,
        "servers_file": os.path.abspath(args.servers), "servers_parsed": len(servers),
        "tcp_info_format": FORMAT_DESCRIPTION,
        "units": {
            "t_s": "s since transfer start (monotonic clock)", "timestamp_unix": "s (wall clock)",
            "goodput_bps": "bit/s = d_bytes_acked_B*8/dt_s", "snd_cwnd_seg": "segments",
            "cwnd_B": "bytes = snd_cwnd*snd_mss", "srtt_us/rttvar_us/min_rtt_us/rto_us": "microseconds",
            "*_B": "bytes", "*_seg": "segments", "*_Bps": "bytes/s", "*_us": "microseconds",
            "d_*": "difference vs previous sample (cumulative counters)",
            "loss proxy": "d_retrans_seg = Δtcpi_total_retrans per interval",
        },
    }
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    selected, attempts, shortfall = select_destinations(servers, args, rng, out_dir)
    meta.update(selected_destinations=[{k: v for k, v in s.items() if k != "port_order"} for s in selected],
                selection_attempts=attempts, destination_shortfall=shortfall)
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)
    with open(os.path.join(out_dir, "destinations.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dest_index", "host", "port", "continent", "country", "site", "provider", "gbps"])
        for i, s in enumerate(selected, 1):
            w.writerow([i, s["host"], s["port"], s["continent"], s["country"], s["site"],
                        s["provider"], s["gbps"]])
    if not selected:
        log("ERROR: no destination could be validated; nothing to run")
        return 1

    est = args.reps * len(selected) * len(args.algorithms) * (args.duration + args.pause + 3)
    log("Running %d reps x %d destinations x %d algorithms (~%.0f min)"
        % (args.reps, len(selected), len(args.algorithms), est / 60))
    run_experiments(selected, args, out_dir)
    meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
    if "gentle_reno" in args.algorithms:
        meta["gentle_reno_bpf_after"] = bpf_snapshot()
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)

    analysis.analyze(out_dir, args.rep_dest, args.warmup)
    runs = analysis.load_runs(out_dir)
    ok = sum(1 for r in runs if r["status"] in analysis.USABLE)
    log("Finished: %d/%d runs usable, %d destination shortfall. Results in %s"
        % (ok, len(runs), shortfall, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
