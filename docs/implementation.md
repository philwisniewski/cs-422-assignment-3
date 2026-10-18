# Gentle Reno: eBPF implementation (Assignment 3, Option 1)

The congestion control algorithm designed in Assignment 2, Q3(b) (`docs/a2_pseudocode.txt`) is implemented as an **eBPF `struct_ops` program** (`bpf/gentle_reno.bpf.c`). It registers a kernel `tcp_congestion_ops` named `gentle_reno`, which is selected on the data socket exactly like `cubic`, `reno`, or `bbr`:

```python
sock.setsockopt(IPPROTO_TCP, TCP_CONGESTION, b"gentle_reno")   # before connect()
```

The Python client (`iperf3_client.py`, `_connect`) does this, then reads the algorithm back with `getsockopt(TCP_CONGESTION)`. The algorithm actually used is recorded in every sample row (`algorithm_actual`) and in every table.

## How the kernel drives it

The Linux TCP stack keeps doing everything except the congestion-window policy: ACK processing, loss detection (dup ACKs, SACK, RACK, RTO timers), fast recovery (PRR), retransmission, and RTT estimation (srtt/rttvar/RTO). It calls our hooks at the decision points:

| Hook | Called when | What Gentle Reno does | Signal used |
|---|---|---|---|
| `init` → `gr_init` | connection established | reset per-connection state, start the first round | – |
| `ssthresh` → `gr_ssthresh` | **once per loss episode**: entering fast recovery (3 dup ACKs / SACK / RACK) or loss state (RTO) | return `max(cwnd·7/10, 2)` | **loss** |
| `cong_avoid` → `gr_cong_avoid` | every ACK that advances `snd_una`, outside recovery | slow start: +1 per ACKed packet; otherwise +1 packet per 2·cwnd ACKed packets (= +0.5/RTT) | **RTT** (per-RTT growth via ACK clocking) |
| `pkts_acked` → `gr_pkts_acked` | every ACK, with its RTT sample | track min RTT; at the end of each round trip, evaluate the slow-start exit rules | **RTT + throughput** |
| `set_state` → `gr_set_state` | CA state changes | after an RTO (`TCP_CA_Loss`), re-arm the slow-start exit | loss (timeout) |
| `undo_cwnd` → `gr_undo_cwnd` | the kernel proves a loss was spurious (DSACK, timestamps) | restore `max(cwnd, prior_cwnd)` (same as Reno) | loss |

### Loss
`gr_ssthresh` returns 0.7·cwnd. During fast recovery the kernel's PRR lowers cwnd smoothly to that value. After recovery cwnd = ssthresh, so the net effect is `cwnd ← 0.7·cwnd`, once per loss episode. That gives "at most one cut per RTT" for free: the kernel calls `ssthresh` only when it *enters* recovery, not for every lost packet. On an RTO the kernel sets cwnd = 1 (in flight + 1) after calling `ssthresh`, so slow start restarts toward 0.7·old cwnd, matching the pseudocode.

**Verified on a live connection:** on a shallow-buffer test path, the measured `ssthresh / cwnd-before-loss` ratio was 0.70–0.73 for `gentle_reno`, 0.50–0.55 for `reno`, and about 0.7 for `cubic`.

### RTT
- Growth is per round trip: `tcp_cong_avoid_ai(tp, 2·cwnd, acked)` adds 1 packet after 2·cwnd packets are ACKed. A window's worth of ACKs arrives once per RTT, so this is +0.5 packet/RTT.
- `gr_pkts_acked` keeps `min_rtt_us` (connection minimum) and `round_min_rtt_us` (minimum within the current round). At the end of a slow-start round it leaves slow start if `round_min_rtt > min_rtt + max(4 ms, min_rtt/8)`, which means a queue has started to build.

### Throughput
At the end of every slow-start round, `bw = (tp->delivered − delivered at round start) · 10⁶ / round duration`, in packets/s from the kernel's delivered counter and µs timestamps. If bw hasn't grown by at least 25% over the best value (`full_bw`) for 3 consecutive rounds, the pipe is full and slow start ends.

**Leaving slow start** sets `ssthresh = cwnd` with no cut, so the window reached is kept and growth continues at +0.5/RTT.

**Rounds:** a round starts by recording `end_seq = snd_nxt`. It ends when `snd_una` passes `end_seq`, which is when everything sent at the start of the round has been ACKed. This takes one RTT and is the same round definition the kernel's CUBIC HyStart uses.

## Initial state and parameters

| | Value | Where |
|---|---|---|
| cwnd | 10 packets (kernel initial window, IW10) | kernel default |
| ssthresh | ∞ (`TCP_INFINITE_SSTHRESH`), so the connection starts in slow start | kernel default |
| min_rtt, round_min_rtt | ∞ (`0xffffffff`) | `gr_init` |
| full_bw, flat_rounds | 0 | `gr_init` |
| round | starts at `snd_nxt`, `tcp_mstamp`, `delivered` at init | `gr_init` / `start_round` |
| β | 7/10 | `BETA_NUM`, `BETA_DEN` |
| a | 1/2 packet per RTT | `AI_DIVISOR = 2` |
| min cwnd after a cut | 2 | `MIN_CWND` |
| D (RTT rise to exit slow start) | max(4 ms, min_rtt/8) | `SS_DELAY_MIN_US`, `SS_DELAY_SHIFT` |
| Throughput plateau | < 1.25× growth for 3 rounds | `SS_BW_GROWTH_*`, `SS_FLAT_ROUNDS` |

Per-connection state is 32 bytes in `icsk_ca_priv`, within the kernel's 104-byte limit. All arithmetic is integer, because BPF and the kernel have no floating point.

Global counters (`stat_init`, `stat_loss_events`, `stat_ss_exit_delay`, `stat_ss_exit_bw`, `stat_undo`) can be read with `sudo make -C bpf stats`. `run_experiments.py` stores a snapshot before and after the experiments in `metadata.json` (`gentle_reno_bpf_before` / `_after`).

## Changes from the Assignment 2 pseudocode

1. **The RTT rule uses the minimum RTT sample in the round instead of srtt.** srtt is an exponentially smoothed average that lags several RTTs behind and is inflated by delayed-ACK and jitter outliers. The minimum of a round's samples reacts within one round and ignores single noisy samples, which is the same choice Linux HyStart makes. The threshold D is unchanged.
2. **The slow-start exit is evaluated at most once per slow-start phase** (`ss_done`), and it's re-armed after an RTO (`set_state` → `TCP_CA_Loss`). The pseudocode left this implicit.
3. **Cut and recovery mechanics come from the kernel.** The pseudocode said "cwnd ← ssthresh; fast recovery as in Reno/NewReno". In the kernel, `ssthresh()` only supplies the target, and PRR (Proportional Rate Reduction, RFC 6937) moves cwnd to it during recovery. The end state is the same as the pseudocode.
4. **Undo of spurious cuts.** The kernel requires an `undo_cwnd` hook. It restores the pre-loss window when a retransmission proves unnecessary, which the pseudocode didn't cover. Reno does the same.
5. **Growth only when cwnd-limited.** Like every kernel algorithm, cwnd grows only when the sender is actually using the window (`tcp_is_cwnd_limited` logic). This prevents window inflation when the application or receiver window is the real limit.
6. **Fixed-point parameters:** β = 7/10 in integer arithmetic, and a = 0.5 implemented as "+1 per 2·cwnd ACKs".

## Requirements

- Linux ≥ 5.13 (BPF `tcp_congestion_ops` with the `tcp_slow_start` / `tcp_cong_avoid_ai` kfuncs). Tested on **[fill in: `uname -r` of your machine]**. Also developed and tested on 6.18.44.
- Kernel config: `CONFIG_BPF_SYSCALL=y`, `CONFIG_BPF_JIT=y`, `CONFIG_DEBUG_INFO_BTF=y` (the file `/sys/kernel/btf/vmlinux` must exist), `CONFIG_TCP_CONG_ADVANCED=y`, and `CONFIG_TCP_CONG_BBR` for the BBR comparison.
- Tools: clang ≥ 12, libbpf headers (`libbpf-dev`), bpftool (`linux-tools-$(uname -r)` or `linux-tools-generic`).
- Root, or CAP_BPF + CAP_NET_ADMIN, to register the program and select non-default algorithms.

## Limitations of the implementation

- No pacing. Like Reno, it sends at ACK-clock speed and relies on the kernel's default pacing behaviour.
- `delivered` counts packets, not bytes, so the throughput estimate is in packets/s. That's fine for a growth ratio.
- The slow-start exit can fire early on a path with noisy RTT. After that, growth is only +0.5 packet/RTT, so short tests (10 s) may end before the window reaches the buffer limit. As a result, in 10-second tests Gentle Reno can show lower RTT and fewer losses than it would over a long transfer.
