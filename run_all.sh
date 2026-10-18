#!/usr/bin/env bash
# Build + register gentle_reno (eBPF), then run the full experiment pipeline.
#
#   sudo ./run_all.sh                                 # defaults: servers.csv, 10 dests, 10 s, 5 reps
#   sudo ./run_all.sh -n 8 -t 10 -r 5 --seed 42       # any run_experiments.py options
#
# gentle_reno stays registered afterwards; remove it with: sudo make -C bpf unload
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (registering eBPF struct_ops and selecting bbr/gentle_reno need it)"; exit 1
fi

echo "== host";  uname -a
echo "== kernel features"
for f in /sys/kernel/btf/vmlinux; do test -e $f && echo "BTF: $f" || { echo "missing $f (need CONFIG_DEBUG_INFO_BTF)"; exit 1; }; done
modprobe tcp_bbr 2>/dev/null || true

echo "== build + register gentle_reno"
make -C bpf
make -C bpf load

ARGS=("$@")
if ! printf '%s\n' "${ARGS[@]:-}" | grep -q -- '--servers'; then
  ARGS=(--servers servers.csv "${ARGS[@]}")
fi
echo "== run_experiments.py ${ARGS[*]}"
python3 run_experiments.py "${ARGS[@]}"

echo "== gentle_reno counters"
make -s -C bpf stats
