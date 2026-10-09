# Demo dashboard

A web page that runs the whole demo. One command starts the controller, the Mininet network and the
three content servers; the page then shows what the controller is doing and lets you trigger every
scenario from the browser.

![Congested-path scenario: the flood fills the fast path, h1 is routed through s3](preview.png)

*Rehearsal mode during the congested-path scenario: the flood fills the fast path, so h1 is routed through s3.*

## Start it

From the project folder, with nothing else running (it cleans up old Mininet itself):

```bash
cd ~/sdn-content-delivery
sudo python3 dashboard/demo.py
```

Open **http://localhost:8080** in the VM's browser. Startup takes about 15 seconds (controller,
switches, then the three servers reporting UP). The terminal keeps the usual `mininet>` prompt as a
backup; type `exit` there to stop everything.

| Option | Meaning |
|---|---|
| `--policy static` / `round_robin` / `least_loaded` | Server policy to start with (default `least_loaded`) |
| `--path-policy delay` / `congestion` | Path policy to start with (default `congestion`) |
| `--port 8081` | Use another port if 8080 is busy |
| `--host 0.0.0.0` | Open the page from another computer, e.g. the Mac, at `http://<VM IP>:8080` |
| `--sim` | Simulated network: no sudo, no Mininet (see below) |

Every session's logs and CSVs are saved in `results/demo/<date_time>/`: one log per controller start,
`decisions.csv`, `h1.csv`…`h3.csv`, server logs and flood output.

## What's on the page

**Live demo tab**

- **Network**: link width and the percentage come from the controller's own port-statistics
  measurements; red means at least 70% (the controller's congestion threshold) or down. Each open
  connection is drawn along its real path in the colour of the server it was given. The strip under
  the map explains the latest decision in words.
- **Controls**: the four scenarios from the experiment runner, plus manual controls: send requests
  to the VIP, kill or restart a server, cut or restore any core link, start a UDP flood, change the
  controller policy, reset the network.
- **Server cards**: process state, the controller's view (UP or not reporting), active connections
  and time since the last UDP load report.
- **Over time**: link use on the fast and slow paths, and the time each request took to get its
  file, coloured by server. Dotted lines mark every action you took, so cause and effect line up.
- **Controller and events**: the controller's own log lines (`[LINK]`, `[HEALTH]`, decisions) next to
  your actions.
- **Latest controller decisions**: one row per new connection, with the server loads and link use
  the controller saw at that moment.

**Results tab**: the headline comparisons and graphs from the newest batch in
`results/experiments/`, so the measured numbers are one click away during the viva.

## Suggested demo (about 10 minutes)

1. **Normal operation.** Send `all three` × 3 × `large.bin`. Each client lands on a different server;
   point at the decisions table and the loads column.
2. **Link cut.** Run *Link cut*. At 6 s s1–s2 turns red, the connection moves to s1–s3–s4, the slow
   path line rises on the chart, and no request fails.
3. **Congested path.** Run *Congested path*. The flood fills the fast path to 100%; h1 is sent through
   s3. Then *Apply* path policy `delay only` and run it again: h1 now goes into the congested path and
   waits much longer, with retries.
4. **Server crash.** With `least_loaded`, run *Server crash*: srv1's process is killed at 6 s; within
   3 s the controller logs `[HEALTH] srv1 is DOWN` (no load reports) and new requests go to srv2 and srv3. Apply `static` and run it again: requests keep going to the dead srv1
   and fail.
5. **Results tab.** Close with the measured comparison from the automated runs.

Use **Reset network** between scenarios, and **Clear charts** for a clean timeline.

## Rehearsal without Mininet

```bash
python3 dashboard/demo.py --sim
```

Same page and controls, driven by a simple model of the network. Behaviour matches the real system
(policies, failure detection after 3 s, congestion-aware paths, retries), but the numbers are rough
estimates; a banner says so. Works on any computer with Python 3, including the Mac.

## How it connects to the project

- `demo.py` builds the same topology as `topology/topo.py`, starts `controller/lb_controller.py` the
  same way `tests/run_experiments.py` does, and runs the real `app/server.py` and `app/client.py` in
  the Mininet hosts. Link failures use `net.configLinkStatus`, the flood uses `iperf` from `gen` to
  `sink`, exactly as in the experiments.
- The controller writes a JSON snapshot every second when `LB_STATE_JSON` is set (only `demo.py`
  sets it, so hand runs and the experiment runner are unchanged). It holds server state, per-link
  utilisation and the path each live connection is using.
- Changing the policy restarts the controller with new `LB_POLICY` / `PATH_POLICY` values and clears
  the old rules from the switches. Downloads must finish first.
- The web server listens on localhost only by default, because it runs as root and can kill
  processes.

## Troubleshooting

| What you see | Fix |
|---|---|
| `ryu-manager not found` | Pass `--ryu /path/to/ryu-manager` (default is `~/ryu-env/bin/ryu-manager`) |
| `Test files missing` | `python3 app/make_content.py` |
| Port 8080 in use | `--port 8081` |
| "Controller offline" | The controller stopped; check the newest `controller_N.log` in the session folder |
| A server shows "not reporting" but the process is running | Load reports are delayed (e.g. by a flood on its link); it recovers once reports arrive |
| Policy change refused | Wait for downloads to finish, or press Reset network |
