# Evidence Log — SDN Content Delivery Project

This file records screenshots taken during development and explains what each one proves.
Images are stored in the `images/` folder next to this file.

| Step | Status |
|---|---|
| Step 1 — Environment setup (Mininet + Ryu) | ✅ Done |
| Step 2 — Project topology | ✅ Done |
| Step 3 — TCP content servers and clients | ✅ Done |

Numeric results extracted from the screenshots are collected in [RESULTS_SUMMARY.md](RESULTS_SUMMARY.md).

---

## Step 1 — Environment Setup

### 1.1 Operating system and Python version
![OS and Python version](images/step1_01_os_and_python_version.png)

**Proves:** The development VM runs Ubuntu 26.04 LTS with Python 3.14.4 (ARM64 VM in UTM on macOS).
**Why it matters:** Ryu does not support Python this new, so a separate Python 3.9 environment was needed for the controller (see 1.2).

### 1.2 Python 3.9 environment for Ryu
![Python 3.9 env created](images/step1_02_python39_env_created.png)

**Proves:** Python 3.9.25 was installed with `uv` and an isolated virtual environment (`~/ryu-env`) was created and activated.
**Why it matters:** Mininet runs on the system Python while Ryu runs in its own Python 3.9 environment. The two communicate over OpenFlow (TCP port 6653), so they do not need the same Python version.

### 1.3 Ryu controller running
![Ryu running](images/step1_03_ryu_controller_running.png)

**Proves:** Ryu 4.34 is installed correctly and the `simple_switch_13` controller app starts and waits for switches to connect.

### 1.4 First end-to-end test (single switch)
![pingall single switch](images/step1_04_pingall_single_switch_success.png)

**Proves:** Mininet and Ryu work together. A 1-switch, 3-host network connected to the remote Ryu controller over OpenFlow 1.3 achieved **0% packet loss (6/6 received)**.

---

## Step 2 — Project Topology

Topology: 3 clients (h1–h3) on s1, 3 servers (srv1–srv3) on s4, two paths between them
(fast path via s2 at 2 ms per link, slow backup path via s3 at 10 ms per link).
Server access delays: srv1 = 1 ms, srv2 = 5 ms, srv3 = 10 ms. All links 10 Mbps.
Source: `topology/topo.py`

### 2.1 STP controller handling the looped topology
![STP packet in](images/step2_01_stp_controller_packet_in.png)

**Proves:** The Ryu STP controller (`simple_switch_stp_13`) is running and receiving `packet_in` messages from all 4 switches (DPIDs 1–4).
**Why it matters:** The two paths form a loop (s1–s2–s4–s3–s1). A basic learning switch would cause a broadcast storm, so the STP controller was used for this test to block one path temporarily.

### 2.2 Full connectivity in the project topology
![pingall project topology](images/step2_02_pingall_project_topology_success.png)

**Proves:**
- All 6 hosts can reach each other: **0% dropped (30/30 received)**.
- Link parameters were applied as designed (10 Mbit bandwidth with 1 ms, 2 ms, 5 ms and 10 ms delays shown in the startup output).

### 2.3 Server proximity differences
![proximity ping](images/step2_03_server_proximity_ping.png)

**Proves:** The servers are at genuinely different network distances from the clients.

| Test | Expected RTT | Measured avg RTT |
|---|---|---|
| h1 → srv1 | 2 × (1+2+2+1) = 12 ms | 16.1 ms (13–14 ms after the first packet) |
| h1 → srv3 | 2 × (1+2+2+10) = 30 ms | 38.6 ms |

The first ping to srv1 (20.8 ms) is higher because the switches consult the controller before a flow rule exists. The small extra delay overall is VM processing overhead.
**Why it matters:** This gives the controller a real proximity choice when selecting servers.

---

## Step 3 — TCP Content Servers and Clients

Application: multithreaded TCP server (`app/server.py`), measuring client (`app/client.py`),
shared protocol (`app/protocol.py`), test files (`app/make_content.py`: 1 KB, 1 MB, 5 MB).
Protocol: `GET <file>` → `OK <size> <server>` + data | `PING` → `PONG <server> <active>` | `ERR <code> <msg>`.
Controller during this step: `simple_switch_stp_13` (clients contact server IPs directly).

### 3.1 Network ready
![network ready](images/step3_01_network_ready_pingall.png)

**Proves:** The topology was up with full connectivity (0% dropped, 30/30) before the application was started.

### 3.2 Three content servers running
![servers started](images/step3_02_servers_started_with_names.png)

**Proves:** Three independent server processes (srv1, srv2, srv3) started inside their Mininet hosts and are listening on TCP port 9000.

### 3.3 Tests A–D: downloads, proximity, health check, error handling
![tests A to D](images/step3_03_tests_A_to_D.png)

**Proves:**
- **Test A** — h1 downloaded 1 MB from srv1 (near) 3 times: all complete (1,048,576 bytes each), ~902 ms, ~9.29 Mbps (close to the 10 Mbps link limit).
- **Test B** — the same from srv3 (far): ~973 ms, ~8.62 Mbps. The farther server is measurably slower (higher RTT affects TCP).
- **Test C** — `PING` → `PONG srv2 1`: application-level health check works (~47 ms, two round trips: TCP handshake + request/reply).
- **Test D** — request for a missing file → `ERR 404 file not found`, handled cleanly by both sides.

### 3.4 Test E: concurrent clients on one server
![concurrent clients](images/step3_04_concurrent_clients.png)

**Proves:**
- **Concurrency:** srv1's log shows `active=2` and `active=3`, so one server served three clients at the same time (thread per connection).
- **Congestion on a shared path:** a 5 MB download alone takes ~4.4 s at ~9.5 Mbps. With three clients sharing the same 10 Mbps path to srv1, each got only ~3.1–3.6 Mbps and needed ~11.8–13.5 s (about 3× slower). Their combined rate stays near 10 Mbps.
- **Why it matters:** This is the baseline problem the SDN controller must solve: all clients on one server and one path share the bottleneck, while srv2, srv3 and the second path sit unused.
- Results were also saved as CSV: `results/step3_h1.csv`, `results/step3_h2.csv`, `results/step3_h3.csv`.

---

## Issues Encountered and Fixes

Useful for the "challenges faced" section of the report and for the viva.

### Issue 1 — `uv: command not found`
![uv not found](images/issue_01_uv_command_not_found.png)

**Cause:** `uv` was installed, but the current terminal's PATH was not refreshed.
**Fix:** `source $HOME/.local/bin/env`

### Issue 2 — Ryu installation failed (`metadata-generation-failed`)
![ryu install failed](images/issue_02_ryu_install_metadata_failed.png)

**Cause:** Ryu 4.34 is an older package. Its installer needs the `pbr` helper and older `setuptools`/`wheel` versions, which were missing because build isolation was disabled.
**Fix:**
```bash
pip install setuptools==67.6.1 wheel==0.43.0 pbr
pip install --no-build-isolation ryu eventlet==0.30.2
```

### Issue 3 — All pings failed in the project topology
![pingall failed](images/issue_03_pingall_failed_no_controller.png)
![ryu terminated](images/issue_03b_ryu_terminated_by_cleanup.png)

**Cause:** `sudo mn -c` (Mininet cleanup) also kills any running `ryu-manager` process. It was run after Ryu had started, so the switches had no controller.
**Fix:** Always use this order:
1. `sudo mn -c`
2. Start Ryu
3. Start the Mininet topology

### Issue 4 — Mininet replaced host names in command arguments
![name substitution](images/issue_04_mininet_name_substitution.png)

**Cause:** The Mininet CLI replaces any word that exactly matches a host name with that host's IP. `--name srv1` became `--name 10.0.0.11`, so the logs showed IPs instead of names.
**Fix:** Write the argument with `=` so it is not a separate word: `--name=srv1`, `--name=h1`.

### Issue 5 — "Concurrent" test actually ran one client at a time
![not concurrent](images/issue_05_test_not_concurrent.png)

**Cause:** Each 5 MB download finished in ~4.4 s, faster than the next command could be typed. srv1's log showed only `active=1`, and each client got the full ~9.5 Mbps.
**Fix:** Make background clients download several times in a row (`--count 5 --interval 0`) so the downloads overlap. Lesson: always check server-side logs to confirm that a concurrency test really overlapped.
