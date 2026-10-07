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
