# Premium-regime filter: chosen in-sample (2019-2022), judged out-of-sample (2023-2026)


## NIFTY  (condor, hold 5, sd 1.0, spread 1.0 pts/leg; 2019-03-11 .. 2026-09-18)

| filter (trailing-60d median premium >=) | in-sample 2019-22 | OUT-OF-SAMPLE 2023-26 | OOS 2024-26 only (thin era) |
|---|---|---|---|
| none (always trade) |   8.05% DD   5.0% Sh  1.37 n=100 |   2.87% DD   4.2% Sh  0.72 n= 53 |   1.16% DD   4.2% Sh  0.28 n= 39 |
| >= 0 pts |   7.66% DD   5.5% Sh  1.33 n= 98 |   2.91% DD   4.6% Sh  0.74 n= 49 |   1.18% DD   4.6% Sh  0.29 n= 35 |
| >= 1 pts |   7.63% DD   6.0% Sh  1.37 n= 93 |   3.58% DD   3.6% Sh  1.06 n= 45 |   2.08% DD   3.6% Sh  0.60 n= 31 |
| >= 2 pts |   8.16% DD   5.0% Sh  1.49 n= 87 |   2.42% DD   3.6% Sh  0.78 n= 35 |   0.70% DD   3.6% Sh  0.23 n= 22 |
| >= 3 pts |   7.33% DD   4.6% Sh  1.38 n= 76 |   1.03% DD   2.2% Sh  0.50 n=  9 |   0.83% DD   2.2% Sh  0.36 n=  7 |
| >= 4 pts |   5.09% DD   4.7% Sh  1.06 n= 59 |   0.00% DD   0.0% Sh  0.00 n=  0 |   0.00% DD   0.0% Sh  0.00 n=  0 |

theta chosen on 2019-22 data only: **>= 2 pts** -> OOS   2.42% DD   3.6% Sh  0.78 n= 35  |  unfiltered OOS   2.87% DD   4.2% Sh  0.72 n= 53

## BANKNIFTY  (condor, hold 10, sd 1.0, spread 1.5 pts/leg; 2019-03-11 .. 2026-09-18)

| filter (trailing-60d median premium >=) | in-sample 2019-22 | OUT-OF-SAMPLE 2023-26 | OOS 2024-26 only (thin era) |
|---|---|---|---|
| none (always trade) |  11.80% DD   9.1% Sh  1.71 n= 83 |   0.91% DD   9.0% Sh  0.17 n= 59 |  -1.31% DD   9.0% Sh -0.16 n= 38 |
| >= 0 pts |   8.68% DD  10.9% Sh  1.23 n= 81 |   3.52% DD   6.2% Sh  0.66 n= 52 |   1.94% DD   6.1% Sh  0.40 n= 31 |
| >= 1 pts |  11.42% DD   9.2% Sh  1.66 n= 80 |   3.90% DD   8.5% Sh  0.76 n= 48 |   2.59% DD   8.5% Sh  0.55 n= 27 |
| >= 2 pts |  10.59% DD   9.2% Sh  1.55 n= 77 |   2.77% DD   7.2% Sh  0.65 n= 40 |   1.11% DD   7.2% Sh  0.33 n= 19 |
| >= 3 pts |   9.83% DD   8.9% Sh  1.44 n= 71 |   2.88% DD   4.8% Sh  0.72 n= 34 |   1.18% DD   4.8% Sh  0.41 n= 13 |
| >= 4 pts |   7.23% DD   9.1% Sh  1.16 n= 64 |   1.42% DD   3.9% Sh  0.53 n= 14 |   0.00% DD   0.0% Sh  0.00 n=  0 |

theta chosen on 2019-22 data only: **>= 1 pts** -> OOS   3.90% DD   8.5% Sh  0.76 n= 48  |  unfiltered OOS   0.91% DD   9.0% Sh  0.17 n= 59

## FINNIFTY  (condor, hold 10, sd 1.0, spread 2.0 pts/leg; 2021-01-11 .. 2026-09-18)

| filter (trailing-60d median premium >=) | in-sample 2019-22 | OUT-OF-SAMPLE 2023-26 | OOS 2024-26 only (thin era) |
|---|---|---|---|
| none (always trade) |  11.43% DD   8.0% Sh  1.93 n= 43 |   1.01% DD  15.6% Sh  0.22 n= 68 |  -1.65% DD  15.6% Sh -0.29 n= 47 |
| >= 0 pts |   6.34% DD   8.7% Sh  0.99 n= 41 |   1.92% DD  15.3% Sh  0.38 n= 64 |  -0.81% DD  15.3% Sh -0.13 n= 44 |
| >= 1 pts |   6.34% DD   8.7% Sh  0.99 n= 41 |   3.79% DD   8.5% Sh  0.78 n= 60 |   1.69% DD   8.5% Sh  0.39 n= 40 |
| >= 2 pts |   6.34% DD   8.7% Sh  0.99 n= 41 |   3.79% DD   6.4% Sh  0.82 n= 54 |   1.68% DD   6.4% Sh  0.43 n= 34 |
| >= 3 pts |   6.34% DD   8.7% Sh  0.99 n= 41 |   3.82% DD   4.8% Sh  0.85 n= 48 |   1.73% DD   4.8% Sh  0.46 n= 28 |
| >= 4 pts |   9.66% DD   5.3% Sh  1.78 n= 38 |   1.93% DD   7.6% Sh  0.47 n= 44 |  -0.52% DD   7.6% Sh -0.12 n= 23 |

theta chosen on 2019-22 data only: **>= 4 pts** -> OOS   1.93% DD   7.6% Sh  0.47 n= 44  |  unfiltered OOS   1.01% DD  15.6% Sh  0.22 n= 68

## Where each index's premium stands today

| index | as of | ATM-IV30 % | premium today (pts) | trailing-60d median (pts) | chosen theta | filter says |
|---|---|---|---|---|---|---|
| NIFTY | 2026-09-18 | 10.36 | +3.21 | +0.85 | 2 | STAND ASIDE |
| BANKNIFTY | 2026-09-18 | 11.84 | +2.01 | -0.14 | 1 | STAND ASIDE |
| FINNIFTY | 2026-09-18 | 19.37 | +8.07 | +6.02 | 4 | TRADE |