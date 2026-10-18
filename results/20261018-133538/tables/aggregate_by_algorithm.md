### Aggregate throughput and RTT per algorithm (all destinations and repetitions)

| algorithm | algorithm_actual | runs | destinations | samples | goodput_mean_mbps | goodput_median_mbps | goodput_p5_mbps | goodput_p95_mbps | srtt_mean_ms | srtt_median_ms | srtt_p95_ms | rtt_inflation_mean_ms | retrans_rate_mean_pct | retrans_per_run_mean | rto_per_run_mean |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cubic | cubic | 48 | 10 | 2400 | 132.17 | 76.68 | 0.12 | 394.43 | 137.02 | 130.62 | 250.75 | 7.29 | 1.63 | 1120.06 | 0.02 |
| reno | reno | 47 | 10 | 2350 | 104.02 | 53.28 | 0.12 | 322.68 | 139.16 | 132.86 | 253.10 | 7.63 | 1.86 | 1044.00 | 0.00 |
| bbr | bbr | 48 | 10 | 2400 | 171.19 | 108.42 | 0.12 | 633.61 | 146.47 | 135.80 | 270.14 | 14.84 | 5.09 | 7759.90 | 0.04 |
| gentle_reno | gentle_reno | 47 | 10 | 2350 | 25.52 | 10.10 | 1.27 | 74.82 | 135.61 | 130.62 | 247.84 | 4.46 | 0.01 | 16.55 | 0.00 |
