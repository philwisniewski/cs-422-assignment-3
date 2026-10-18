### Algorithm comparison: mean (std) over repetitions of per-run means

| destination | cubic_runs | cubic_goodput_mbps | cubic_goodput_std | cubic_srtt_ms | cubic_srtt_std | cubic_retrans_pct | reno_runs | reno_goodput_mbps | reno_goodput_std | reno_srtt_ms | reno_srtt_std | reno_retrans_pct | bbr_runs | bbr_goodput_mbps | bbr_goodput_std | bbr_srtt_ms | bbr_srtt_std | bbr_retrans_pct | gentle_reno_runs | gentle_reno_goodput_mbps | gentle_reno_goodput_std | gentle_reno_srtt_ms | gentle_reno_srtt_std | gentle_reno_retrans_pct | best_goodput | lowest_rtt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10.77.3.2:5201 | 1 | 18.43 | 0.00 | 335.82 | 0.00 | 0.00 | 1 | 17.98 | 0.00 | 1014.60 | 0.00 | 2.00 | 1 | 18.39 | 0.00 | 92.29 | 0.00 | 0.00 | 1 | 18.41 | 0.00 | 94.50 | 0.00 | 0.00 | cubic | bbr |
| 10.77.1.2:5201 | 1 | 190.95 | 0.00 | 135.54 | 0.00 | 0.52 | 1 | 190.91 | 0.00 | 139.87 | 0.00 | 0.00 | 1 | 190.96 | 0.00 | 7.13 | 0.00 | 0.00 | 1 | 190.45 | 0.00 | 17.06 | 0.00 | 0.00 | bbr | bbr |
| 10.77.2.2:5201 | 1 | 46.82 | 0.00 | 246.82 | 0.00 | 0.12 | 1 | 46.84 | 0.00 | 606.63 | 0.00 | 0.00 | 1 | 46.76 | 0.00 | 47.11 | 0.00 | 0.00 | 1 | 44.96 | 0.00 | 46.73 | 0.00 | 0.00 | reno | gentle_reno |
| ALL | 3 | 85.40 | 75.53 | 239.39 | 81.93 | 0.21 | 3 | 85.24 | 75.64 | 587.04 | 357.38 | 0.67 | 3 | 85.37 | 75.55 | 48.85 | 34.79 | 0.00 | 3 | 84.61 | 75.62 | 52.76 | 31.90 | 0.00 | cubic | bbr |
