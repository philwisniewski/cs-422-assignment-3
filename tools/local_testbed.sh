#!/usr/bin/env bash
# Local test network for checking the pipeline (not used for the real experiment).
#
# Creates network namespaces, each running stock `iperf3 -s`, behind a shaped
# veth link, and writes a matching server list. Uses netem (delay + rate) when
# the kernel has it, otherwise a token-bucket (tbf) bottleneck only.
#
#   sudo tools/local_testbed.sh up   [servers_file]   (default: local_servers.csv)
#   sudo tools/local_testbed.sh down
set -euo pipefail

#        name    subnet  rate(mbit)  one-way-delay(ms)  queue(ms of buffering)
PATHS=( "lan     1       200         2                  10"
        "wan     2       50          20                 80"
        "slow    3       20          40                 300" )

down() {
  for p in "${PATHS[@]}"; do
    set -- $p
    for ns in "cc_$1" "ccr_$1"; do
      ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
      ip netns del "$ns" 2>/dev/null || true
    done
    ip link del "vcc_$1" 2>/dev/null || true
  done
}

# Topology per path:  this host --veth-- router ns --veth-- server ns (iperf3 -s)
# The bottleneck is on the router's egress toward the server, so packets are
# dropped in the network (real losses), not in the sender's own qdisc, where
# TCP Small Queues would just throttle the sender.
up() {
  local out=${1:-local_servers.csv}
  command -v iperf3 >/dev/null || { echo "install iperf3 first (server side only)"; exit 1; }
  down
  local have_netem=0
  ip link add cc_probe type dummy 2>/dev/null && {
    tc qdisc add dev cc_probe root netem delay 1ms 2>/dev/null && have_netem=1
    ip link del cc_probe; }
  echo "IP/HOST,PORT,GB/S,CONTINENT,COUNTRY,SITE,PROVIDER" > "$out"
  for p in "${PATHS[@]}"; do
    set -- $p
    local n=$1 i=$2 rate=$3 delay=$4 qms=$5 ns="cc_$1" rt="ccr_$1"
    ip netns add "$rt"; ip netns add "$ns"
    ip netns exec "$rt" sysctl -qw net.ipv4.ip_forward=1
    ip link add "vcc_$n" type veth peer name r0 netns "$rt"
    ip netns exec "$rt" ip link add r1 type veth peer name eth0 netns "$ns"
    ip addr add "10.76.$i.1/24" dev "vcc_$n"; ip link set "vcc_$n" up
    ip route replace "10.77.$i.0/24" via "10.76.$i.2"
    ip netns exec "$rt" ip addr add "10.76.$i.2/24" dev r0
    ip netns exec "$rt" ip addr add "10.77.$i.1/24" dev r1
    ip netns exec "$rt" ip link set r0 up; ip netns exec "$rt" ip link set r1 up
    ip netns exec "$ns" ip addr add "10.77.$i.2/24" dev eth0
    ip netns exec "$ns" ip link set eth0 up; ip netns exec "$ns" ip link set lo up
    ip netns exec "$ns" ip route add default via "10.77.$i.1"
    # router buffer: qms milliseconds at the bottleneck rate, in packets
    local limit=$(( rate * 1000000 / 8 / 1500 * qms / 1000 + 2 ))
    if [ $have_netem = 1 ]; then
      ip netns exec "$rt" tc qdisc add dev r1 root netem delay "${delay}ms" rate "${rate}mbit" limit "$limit"
      ip netns exec "$rt" tc qdisc add dev r0 root netem delay "${delay}ms"
    else
      ip netns exec "$rt" tc qdisc add dev r1 root tbf rate "${rate}mbit" burst 32kb latency "${qms}ms"
    fi
    ip netns exec "$ns" iperf3 -s -p 5201 -D
    echo "10.77.$i.2,5201,$(awk "BEGIN{print $rate/1000}"),Local,XX,$n,testbed" >> "$out"
  done
  echo "testbed up (netem=$have_netem); server list: $out"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) down ;;
  *) echo "usage: $0 up [servers_file] | down"; exit 1 ;;
esac
