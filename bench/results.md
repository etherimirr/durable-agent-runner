Machine: arm64, 14 cores, Darwin 24.5.0, Python 3.12.12

| Lease | Trials | Median takeover | p95 | Max |
|---|---|---|---|---|
| 0.5 s | 10 | 0.44 s | 0.49 s | 0.49 s |
| 1 s | 10 | 0.88 s | 1.01 s | 1.01 s |
| 2 s | 10 | 1.70 s | 2.04 s | 2.04 s |
| 5 s | 10 | 4.06 s | 4.72 s | 4.72 s |

| Workers | Runs | Runs per second |
|---|---|---|
| 1 | 200 | 6.7 |
| 2 | 200 | 13.5 |
| 4 | 200 | 25.9 |
| 8 | 200 | 50.8 |
