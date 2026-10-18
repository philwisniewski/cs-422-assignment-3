# CS 422 Assignment 3, Option 1: Gentle Reno as an eBPF congestion control

This repo implements the algorithm designed in Assignment 2 Q3(b) as an **eBPF `struct_ops` TCP congestion control** (`bpf/gentle_reno.bpf.c`). It then compares the algorithm against CUBIC, Reno, and BBR using the Assignment 2 iperf3 client and analysis pipeline.

- Design (A2 pseudocode): [`docs/a2_pseudocode.txt`](docs/a2_pseudocode.txt)
- How the implementation uses RTT, loss, and throughput; initial state; parameters; and changes from the pseudocode: [`docs/implementation.md`](docs/implementation.md)
- Assignment 2 repo: https://github.com/philwisniewski/cs-422-assignment-2

## Layout

| Path | What |
|---|---|
| `bpf/gentle_reno.bpf.c` | the congestion control (hooks `gr_init`, `gr_ssthresh`, `gr_cong_avoid`, `gr_pkts_acked`, `gr_set_state`, `gr_undo_cwnd`) |
| `bpf/Makefile` | build / load / unload / status / stats |
| `run_all.sh` | one command: build, register, run every experiment, plot |
| `run_experiments.py` | entry point (from A2): destination selection, runs, analysis. Now defaults to 4 algorithms and 10 s runs, and snapshots the eBPF program's counters |
| `iperf3_client.py` | from-scratch iperf3 client (A2). Selects the algorithm on the data socket before `connect()` and records the algorithm actually used |
| `tcp_info.py` | `TCP_INFO` decoding (A2) |
| `analysis.py` | tables and PDF plots (A2) plus `aggregate_by_algorithm` |
| `servers.csv` | server list from iperf3serverlist.net |
| `tools/local_testbed.sh` | optional local test network (namespaces + router bottleneck + stock `iperf3 -s`) |

## Requirements

- Linux **≥ 5.13** with BTF. Check with `ls /sys/kernel/btf/vmlinux`. Kernel config needs `CONFIG_BPF_SYSCALL`, `CONFIG_BPF_JIT`, and `CONFIG_DEBUG_INFO_BTF`. Ubuntu 22.04 / 24.04 kernels have all three.
- Exact kernel used for the results: **[fill in `uname -r`]** (also recorded in `results/*/metadata.json`)
- Packages (Ubuntu):
  ```bash
  sudo apt install clang llvm libbpf-dev linux-tools-common linux-tools-$(uname -r) python3-numpy python3-matplotlib
  # if linux-tools-$(uname -r) does not exist (custom/WSL kernels), use linux-tools-generic;
  # the Makefile then finds /usr/lib/linux-tools-*/bpftool automatically
  ```
- Root (or CAP_BPF + CAP_NET_ADMIN), to register the program and to select `bbr` / `gentle_reno`.

## Build and load

```bash
make -C bpf                 # generates bpf/vmlinux.h from the running kernel, compiles gentle_reno.bpf.o
sudo make -C bpf load       # bpftool struct_ops register -> "gentle_reno" appears in:
cat /proc/sys/net/ipv4/tcp_available_congestion_control      # ... cubic reno bbr gentle_reno
sudo make -C bpf status
sudo make -C bpf stats      # counters: connections, loss events, slow-start exits (delay / bandwidth), undos
sudo make -C bpf unload
```

Quick check without the pipeline:
```bash
python3 -c "import socket;s=socket.socket();s.setsockopt(6,13,b'gentle_reno');print(s.getsockopt(6,13,16))"
```

## Run the experiments

```bash
sudo ./run_all.sh -n 10 -t 10 -i 0.2 -r 5 --seed 42
```

This is equivalent to `make -C bpf load` followed by:
```bash
sudo python3 run_experiments.py --servers servers.csv -n 10 -t 10 -i 0.2 -r 5 --seed 42
```

| Option | Default | Meaning |
|---|---|---|
| `-n` | 10 | destinations (random from `servers.csv`; failures replaced and logged) |
| `-t` | 10 | seconds per test (the assignment asks for 10 s) |
| `-i` | 0.2 | sampling interval in seconds (goodput + TCP_INFO from the data socket) |
| `-r` | 5 | repetitions of every destination × algorithm |
| `-a` | `cubic,reno,bbr,gentle_reno` | algorithms |
| `--seed` | random (recorded) | destination selection seed |

All other options are the same as in Assignment 2 (`python3 run_experiments.py --help`).

**Test order:** for each repetition, for each destination, all four algorithms run back to back. The order is rotated by `(rep + destination index)`, so no algorithm always goes first. The exact order is in `results/<run>/test_order.csv`, and the repetition count is in `metadata.json`. Every algorithm uses the same destinations, duration, and sampling interval.

**Logged per sample** (`results/<run>/samples/*.csv`): timestamp; goodput (Δ`tcpi_bytes_acked`/Δt); `snd_cwnd`, `snd_ssthresh`; srtt / rttvar / min_rtt / rto; loss signals (Δ`tcpi_total_retrans` per interval, `bytes_retrans`, `lost`, `total_rto` timeouts, `ca_state`, where 3 = Recovery after dup ACKs and 4 = Loss after an RTO); and the algorithm actually used.

## Outputs (`results/<timestamp>/`)

- `plots/q2_timeseries_<alg>.pdf`, `plots/q2_scatter_<alg>.pdf`: the A2 Q2(b) figures for **each of the four algorithms**, same representative destination, shared axis scales
- `plots/q3_timeseries_compare.pdf`, `plots/q3_scatter_compare.pdf`: the four algorithms side by side
- `plots/q3_timeseries_overlay.pdf`, `plots/q1_goodput_timeseries.pdf`, `plots/q3_regimes.pdf`
- `tables/aggregate_by_algorithm.md`: **throughput and RTT aggregate statistics** per algorithm (mean, median, p5/p95 goodput; mean, median, p95 srtt; RTT inflation; retransmissions; timeouts)
- `tables/goodput_summary.md`, `tables/run_summary.md`, `tables/algorithm_comparison.md`
- `metadata.json` (kernel, available/allowed algorithms, arguments, seed, eBPF registration and counters before and after), `test_order.csv`, `selection_log.csv`, `destinations.csv`, `runs.jsonl`, `samples/`

To regenerate the plots and tables only: `python3 run_experiments.py --plot-only results/<run>`

## Local test network (optional)

To check everything works without public servers:
```bash
sudo apt install iperf3
sudo tools/local_testbed.sh up local_servers.csv     # 3 paths: 200 Mb/s LAN, 50 Mb/s WAN, 20 Mb/s slow
sudo ./run_all.sh --servers local_servers.csv -n 3 -t 10 -r 2
sudo tools/local_testbed.sh down
```
Each path is: this host → router namespace (bottleneck: netem delay + rate, or tbf) → server namespace running stock `iperf3 -s`. The bottleneck sits in a separate router so that losses happen in the network. A qdisc on the sender itself would only throttle the sender through TCP Small Queues.

## AI use

**[Fill in your own wording]** for example: AI (Claude) was used to brainstorm the design, draft the eBPF program, scripts, and documentation, and build the local test network. I reviewed the code and verified the behaviour on my machine (loss cut ratio, slow-start exit counters, algorithm reported by the socket).
