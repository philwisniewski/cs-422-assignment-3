### Aggregate throughput and RTT per algorithm (all destinations and repetitions)

| algorithm | algorithm_actual | runs | destinations | samples | goodput_mean_mbps | goodput_median_mbps | goodput_p5_mbps | goodput_p95_mbps | srtt_mean_ms | srtt_median_ms | srtt_p95_ms | rtt_inflation_mean_ms | retrans_rate_mean_pct | retrans_per_run_mean | rto_per_run_mean |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cubic | cubic | 3 | 3 | 150 | 85.40 | 47.84 | 19.03 | 191.99 | 239.39 | 210.94 | 499.44 | 197.53 | 0.21 | 305.67 | 0.00 |
| reno | reno | 3 | 3 | 150 | 85.24 | 47.65 | 6.11 | 191.27 | 587.04 | 560.06 | 1589.56 | 545.13 | 0.67 | 122.33 | 0.00 |
| bbr | bbr | 3 | 3 | 150 | 85.37 | 47.78 | 18.99 | 191.83 | 48.85 | 41.95 | 100.99 | 7.23 | 0.00 | 0.00 | 0.00 |
| gentle_reno | gentle_reno | 3 | 3 | 150 | 84.61 | 47.77 | 18.99 | 191.69 | 52.76 | 45.70 | 104.51 | 10.90 | 0.00 | 0.00 | 0.00 |
