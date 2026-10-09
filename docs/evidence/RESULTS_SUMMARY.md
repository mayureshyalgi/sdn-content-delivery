# Results Summary

All numbers below were read from the test screenshots in `images/` (see `EVIDENCE.md`).
They are the baseline measurements before the smart SDN controller is added.

**Test conditions (Steps 2–3):** all links 10 Mbps; server access delays srv1 = 1 ms, srv2 = 5 ms, srv3 = 10 ms;
fast path via s2 (2 ms per link), backup path via s3 (10 ms per link);
controller = Ryu `simple_switch_stp_13` (no load balancing, clients contact server IPs directly).

---

## 1. Network connectivity

| Test | Result | Source |
|---|---|---|
| Step 1 `pingall` (1 switch, 3 hosts) | 0% dropped, 6/6 | step1_04 |
| Step 2 `pingall` (project topology, 6 hosts) | 0% dropped, 30/30 | step2_02 |
| Step 3 `pingall` (before starting servers) | 0% dropped, 30/30 | step3_01 |

## 2. Network round-trip time (ICMP ping, h1 → server)

| Path | Expected RTT | seq 1 | seq 2 | seq 3 | Avg |
|---|---|---|---|---|---|
| h1 → srv1 (near) | 12 ms | 20.8 ms* | 14.1 ms | 13.4 ms | 16.09 ms |
| h1 → srv3 (far) | 30 ms | 38.3 ms | 39.9 ms | 37.7 ms | 38.63 ms |

\* First packet is slower because the switch asks the controller before a flow rule exists.

## 3. Single-client downloads (1 MB file, h1)

| Request | srv1 (near) response | srv1 throughput | srv3 (far) response | srv3 throughput |
|---|---|---|---|---|
| #1 | 902.48 ms | 9.295 Mbps | 974.58 ms | 8.607 Mbps |
| #2 | 902.79 ms | 9.292 Mbps | 974.63 ms | 8.607 Mbps |
| #3 | 902.23 ms | 9.298 Mbps | 969.38 ms | 8.654 Mbps |
| **Average** | **902.5 ms** | **9.29 Mbps** | **972.9 ms** | **8.62 Mbps** |

**Observation:** the far server is ~70 ms (≈8%) slower per 1 MB download and gives ~7% lower throughput.
All 6 downloads succeeded with the full 1,048,576 bytes.

## 4. Application health check and error handling

| Test | Result |
|---|---|
| `PING` h1 → srv2 | `PONG srv2 1` in 47.2 ms and 48.3 ms (TCP handshake + request/reply ≈ 2 RTT) |
| `GET nothere.txt` h1 → srv1 | `ERR 404 file not found`, client reported the error, server kept running |

## 5. One client at a time vs. three concurrent clients (5 MB file, all to srv1)

### 5a. Non-overlapping downloads (first Test E attempt, issue 5)

| Client | Response | Throughput |
|---|---|---|
| h2 | 4417.35 ms | 9.495 Mbps |
| h3 | 4426.33 ms | 9.476 Mbps |
| h1 | 4415.56 ms | 9.499 Mbps |
| **Average** | **4419.7 ms** | **9.49 Mbps** |

srv1 log: `active=1` for every connection (no overlap).

### 5b. Overlapping downloads (Test E, second attempt)

| Client | #1 | #2 | #3 | #4 | #5 | Avg |
|---|---|---|---|---|---|---|
| h1 response (ms) | 11805.17 | 12492.37 | 12361.38 | – | – | **12219.6** |
| h1 throughput (Mbps) | 3.553 | 3.357 | 3.393 | – | – | **3.43** |
| h2 response (ms) | 4415.67 | 4742.03 | 9733.49 | 12736.18 | 13362.45 | **8998.0** |
| h2 throughput (Mbps) | 9.499 | 8.845 | 4.309 | 3.293 | 3.139 | **5.82** |
| h3 response (ms) | 9339.77 | 12650.05 | 13484.77 | 10569.74 | 4408.14 | **10090.5** |
| h3 throughput (Mbps) | 4.491 | 3.316 | 3.110 | 3.968 | 9.515 | **4.88** |

All 13 downloads succeeded (5,242,880 bytes each). CSV files: `results/step3_h1.csv`, `step3_h2.csv`, `step3_h3.csv`.

srv1 active connections over time (from its log):

| Time | Event | active |
|---|---|---|
| 16:37:46 | h2 starts alone | 1 |
| 16:37:53 | h3 joins | 2 |
| 16:37:59 | h1 joins | 3 |
| 16:37:59 – 16:38:23 | all three downloading | 3 |
| 16:38:29 | h1 and h2 finishing | 2 |
| 16:38:39 | h3's last download alone | 1 |

### Key comparison

| Situation | Response time (5 MB) | Throughput per client |
|---|---|---|
| Client alone on srv1 | ~4.4 s | ~9.5 Mbps |
| 3 clients sharing srv1 and its path | ~11.8–13.5 s | ~3.1–3.6 Mbps |

**Observation:** with all clients on one server and one path, each client gets about one third of the 10 Mbps bottleneck and downloads take about 3× longer, even though srv2, srv3 and the backup path are idle.
This is the **static baseline** that the dynamic SDN controller will be compared against.

---

# Step 4 — Dynamic SDN Controller

Clients connect to the virtual IP 10.0.0.100:9000; core links 20 Mbps, host links 10 Mbps.

## 6. Test F — round-robin (1 MB, h1, 6 requests)

Served srv1, srv2, srv3, srv1, srv2, srv3; 6/6 succeeded; avg 931.4 ms, **9.01 Mbps**
(srv1 ≈ 9.3 Mbps, srv3 ≈ 8.7 Mbps: the proximity effect persists).

## 7. Test H — three concurrent clients, least-loaded (5 MB)

| Client | Avg response | Avg throughput |
|---|---|---|
| h1 | 5359.4 ms | 8.24 Mbps |
| h2 | 4530.4 ms | 9.27 Mbps |
| h3 | 4547.4 ms | 9.25 Mbps |

## 8. Static (Step 3 Test E) vs dynamic (Test H)

| Client | Static | Least-loaded | Speed-up |
|---|---|---|---|
| h1 | 3.43 Mbps | 8.24 Mbps | 2.40× |
| h2 | 5.82 Mbps | 9.27 Mbps | 1.59× |
| h3 | 4.88 Mbps | 9.25 Mbps | 1.90× |
| **Mean** | **4.71 Mbps / 10.44 s** | **8.92 Mbps / 4.81 s** | **1.89×** |

(Superseded by the controlled, simultaneous-start measurements in §14.)

## 9. Test I — server failure

srv2 killed → `[HEALTH] srv2 is DOWN` within 3 s; 4/4 subsequent requests succeeded (9.28 Mbps);
restart → `[HEALTH] srv2 is UP`; 3/3 further requests succeeded. Zero client-visible errors.

---

# Step 5a — Link Failure and Rerouting

## 10. Latency (h1 → srv1, 5 pings)

| Phase | Run 1 | Fixed run | Path |
|---|---|---|---|
| Before | 13.74 ms | 14.65 ms | s1 → s2 → s4 |
| Link s1–s2 down | 53.60 ms | 54.60 ms* | s1 → s3 → s4 |
| Restored | 12.74 ms | 13.26 ms | s1 → s2 → s4 |

\* excluding the first ping (390 ms) that triggered rule reinstallation. Expected: 12 ms fast, 44 ms slow.

## 11. Downloads across the failure (fixed run)

| # | Response | Throughput | Path |
|---|---|---|---|
| 1 | 4417.22 ms | 9.495 Mbps | fast |
| 2 | 4435.51 ms | 9.456 Mbps | **cut mid-transfer** (~219 KB fast, rest backup) |
| 3 | 4565.78 ms | 9.186 Mbps | backup |
| 4 | 4509.74 ms | 9.301 Mbps | backup |
| **Summary** | **4482.1 ms** | **9.36 Mbps** | **4/4, 0 retries** |

---

# Step 5b — Congestion-Aware Routing

## 12. Diagnostic: fast-path capacity

Two 10 Mbps UDP floods across the fast path: h1's 5 MB download fell to **1.345 Mbps (31.2 s)**; the next
two, after the flood ended, ran at 9.51 Mbps.

## 13. Same 25 Mbps flood, congestion-awareness off vs on

| | Run A: `PATH_POLICY=delay` | Run B: `PATH_POLICY=congestion` |
|---|---|---|
| Path for h1 | s1 → s2 → s4 (into the jam) | **s1 → s3 → s4** |
| Failed attempts | **2** | **0** |
| Downloads completed during the flood | **0** | **3** |
| Throughput | 9.44–9.48 Mbps (after flood ended) | **9.19–9.31 Mbps (during flood)** |
| Time until first file | ≈ 25–30 s | ≈ 4.5 s |

---

# Step 6 — Automated Experiments (final results)

Batch `results/experiments/20261008_003510`: 4 scenarios, 8 configurations, **3 repetitions, 24/24 runs
succeeded**. Clients in a run start at the same instant. Time to content (TTC) = full wait for a file,
including failed attempts and retry pauses.

## 14. Summary by configuration

| Scenario | Config | Requests | Completed | Failed | Extra attempts | Mean Mbps | Mean TTC (s) | Max TTC (s) |
|---|---|---|---|---|---|---|---|---|
| concurrent | static | 27 | 27 | 0 | 0 | 3.20 | 13.12 | 13.74 |
| concurrent | round_robin | 27 | 27 | 0 | 0 | 8.05 | 5.37 | 7.52 |
| concurrent | least_loaded | 27 | 27 | 0 | 0 | 7.81 | 5.53 | 7.72 |
| congestion | path_delay | 9 | 9 | 0 | 5 | 8.91* | 16.58 | **42.84** |
| congestion | path_congestion | 9 | 9 | 0 | 0 | 8.90 | 4.74 | **5.44** |
| linkfail | least_loaded | 18 | 18 | 0 | 0 | 9.40 | 4.46 | 4.62 |
| serverfail | static | 36 | **0** | **36** | 108 | – | – | – |
| serverfail | least_loaded | 36 | **36** | 0 | 6 | 7.09 | 6.73 | 13.14 |

\* measured on attempts that succeeded, nearly all after the flood ended; TTC is the meaningful metric here.

The 1-repetition quick pass (batch `20261008_002750`) agreed closely, e.g. concurrent static 3.19 vs
least-loaded 8.19 Mbps; congestion max TTC 40.4 s vs 4.5 s; serverfail static 0/12 vs least-loaded 12/12.

## 15. Headline comparisons

| Experiment | Baseline | Proposed | Improvement |
|---|---|---|---|
| 3 concurrent clients | static: 3.20 Mbps, 13.1 s | round-robin / least-loaded: ~8 Mbps, ~5.4 s | **2.5× throughput, −59% wait** |
| Flood on the fast path | blind: max wait 42.8 s, 5 failed attempts | aware: max wait 5.4 s, 0 failed | **7.9× lower worst-case wait** |
| srv1 dies mid-run | static: 0/36 completed | least-loaded: 36/36 completed | **0% → 100% completed** |
| s1–s2 cut and restored | – | 18/18 completed, 0 retries, 9.4 Mbps | **failure invisible to clients** |

## 16. Interpretation

- **Round-robin ≈ least-loaded under uniform load**: three identical clients on three idle servers is exactly
  where rotation already spreads them perfectly; the ~3% gap is within run-to-run variation. Least-loaded
  pays off when load is uneven, e.g. after a failure.
- **Dynamic concurrent runs reach ~8, not ~9.3 Mbps**, because all three connections start before the next
  2-second monitoring sample and briefly share the fast path; later ones see the congestion and take the
  backup path. The monitoring interval bounds how fast routing reacts.
- **Least-loaded needed 2 extra attempts per repetition in serverfail**: the client whose transfer was on srv1
  retried inside the 3-second detection window, before srv1 was marked DOWN. That is the `DEAD_AFTER`
  trade-off: faster detection risks false alarms (Issue 9).
- **Static loses every request after srv1 fails**, including transfers already in progress: it has no notion
  of server health.
