#!/usr/bin/env python3
"""
Automated experiment runner for the SDN content-delivery project.

For every scenario, configuration and repetition it:
  1. starts the Ryu controller with the chosen LB_POLICY / PATH_POLICY
  2. builds the Mininet topology and waits for all four switches
  3. starts the three content servers and waits until the controller sees them UP
  4. runs the scenario (clients launched at the same instant, failures injected on a timer)
  5. saves every log and CSV, tears everything down
and finally writes summary tables for the whole batch.

Run from the project folder, with nothing else running:
    sudo python3 tests/run_experiments.py                       # everything, 3 repetitions
    sudo python3 tests/run_experiments.py --reps 1              # quick pass
    sudo python3 tests/run_experiments.py --scenarios congestion linkfail

Results go to results/experiments/<timestamp>/.
"""

import argparse
import csv
import os
import pwd
import signal
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "topology"))

from mininet.net import Mininet                    # noqa: E402
from mininet.node import RemoteController, OVSSwitch  # noqa: E402
from mininet.link import TCLink                    # noqa: E402
from mininet.log import setLogLevel                # noqa: E402
from mininet.clean import cleanup                  # noqa: E402
from topo import ContentTopo, CONTROLLER_IP, CONTROLLER_PORT  # noqa: E402

VIP = "10.0.0.100"
SERVERS = ["srv1", "srv2", "srv3"]
FILE = "large.bin"                      # 5 MB

# scenario -> list of (label, LB_POLICY, PATH_POLICY)
SCENARIOS = {
    "concurrent": [("static", "static", "congestion"),
                   ("round_robin", "round_robin", "congestion"),
                   ("least_loaded", "least_loaded", "congestion")],
    "congestion": [("path_delay", "least_loaded", "delay"),
                   ("path_congestion", "least_loaded", "congestion")],
    "linkfail":   [("least_loaded", "least_loaded", "congestion")],
    "serverfail": [("static", "static", "congestion"),
                   ("least_loaded", "least_loaded", "congestion")],
}

DESCRIPTIONS = {
    "concurrent": "h1, h2, h3 start together, 3 x 5 MB each",
    "congestion": "25 Mbps UDP flood gen->sink on the fast path, then h1 downloads 3 x 5 MB",
    "linkfail":   "h1 downloads 6 x 5 MB; link s1-s2 down at 6 s, up at 18 s",
    "serverfail": "h1, h2, h3 download 4 x 5 MB each; srv1 killed at 6 s",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
def default_ryu():
    user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, "ryu-env", "bin", "ryu-manager")


def start_controller(ryu, lb_policy, path_policy, rundir):
    env = dict(os.environ, LB_POLICY=lb_policy, PATH_POLICY=path_policy,
               LB_DECISIONS_CSV=os.path.join(rundir, "decisions.csv"))
    logf = open(os.path.join(rundir, "controller.log"), "w")
    proc = subprocess.Popen([ryu, "controller/lb_controller.py"], stdout=logf,
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
    wait_for_text(os.path.join(rundir, "controller.log"), ["[START]"], 20)
    time.sleep(1)
    return proc, logf


def stop_controller(proc, logf):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    logf.close()


def wait_for_text(path, needles, timeout):
    """Wait until every string in needles appears in the file at path."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = open(path).read()
            if all(n in text for n in needles):
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
def start_servers(net, rundir):
    procs = []
    for name in SERVERS:
        out = open(os.path.join(rundir, f"{name}.log"), "w")
        procs.append(net.get(name).popen(["python3", "app/server.py", f"--name={name}"],
                                         stdout=out, stderr=subprocess.STDOUT))
    return procs


def start_client(net, name, count, rundir, timeout=10):
    out = open(os.path.join(rundir, f"{name}_out.txt"), "w")
    return net.get(name).popen(
        ["python3", "app/client.py", f"--name={name}", "--server", VIP, "--file", FILE,
         "--count", str(count), "--interval", "0", "--timeout", str(timeout),
         "--csv", os.path.join(rundir, f"{name}.csv")],
        stdout=out, stderr=subprocess.STDOUT)


def wait_all(procs, timeout):
    deadline = time.time() + timeout
    for p in procs:
        remaining = max(1, deadline - time.time())
        try:
            p.wait(remaining)
        except subprocess.TimeoutExpired:
            p.kill()


class Events:
    """Timestamped record of injected events, relative to the run's start."""
    def __init__(self, rundir):
        self.t0 = time.time()
        self.f = open(os.path.join(rundir, "events.csv"), "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(["epoch", "t_s", "event"])

    def __call__(self, event):
        now = time.time()
        self.w.writerow([round(now, 3), round(now - self.t0, 2), event])
        self.f.flush()
        log(f"    event: {event}")

    def close(self):
        self.f.close()


def sleep_until(t0, seconds):
    delay = t0 + seconds - time.time()
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_concurrent(net, rundir, ev):
    clients = [start_client(net, h, 3, rundir) for h in ("h1", "h2", "h3")]
    ev("clients started")
    wait_all(clients, 240)


def scenario_congestion(net, rundir, ev):
    sink, gen = net.get("sink"), net.get("gen")
    srv = sink.popen(["iperf", "-s", "-u"], stdout=open(os.path.join(rundir, "iperf_sink.txt"), "w"),
                     stderr=subprocess.STDOUT)
    time.sleep(1)
    flood = gen.popen(["iperf", "-c", sink.IP(), "-u", "-b", "25M", "-t", "40"],
                      stdout=open(os.path.join(rundir, "flood.txt"), "w"), stderr=subprocess.STDOUT)
    ev("flood started (25 Mbps, 40 s)")
    time.sleep(6)                          # let the controller take >= 2 utilisation samples
    client = start_client(net, "h1", 3, rundir)
    ev("h1 started")
    wait_all([client], 240)
    flood.wait(60)
    ev("flood ended")
    srv.kill()


def scenario_linkfail(net, rundir, ev):
    client = start_client(net, "h1", 6, rundir)
    ev("h1 started")
    sleep_until(ev.t0, 6)
    net.configLinkStatus("s1", "s2", "down")
    ev("link s1-s2 down")
    sleep_until(ev.t0, 18)
    net.configLinkStatus("s1", "s2", "up")
    ev("link s1-s2 up")
    wait_all([client], 240)


def scenario_serverfail(net, rundir, ev):
    clients = [start_client(net, h, 4, rundir) for h in ("h1", "h2", "h3")]
    ev("clients started")
    sleep_until(ev.t0, 6)
    net.get("srv1").cmd("pkill -f 'name=srv1'")
    ev("srv1 killed")
    wait_all(clients, 240)


RUNNERS = {"concurrent": scenario_concurrent, "congestion": scenario_congestion,
           "linkfail": scenario_linkfail, "serverfail": scenario_serverfail}


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------
def run_once(ryu, scenario, label, lb_policy, path_policy, rundir):
    os.makedirs(rundir, exist_ok=True)
    cleanup()
    ctrl, ctrl_log = start_controller(ryu, lb_policy, path_policy, rundir)
    net = None
    try:
        net = Mininet(topo=ContentTopo(), switch=OVSSwitch, link=TCLink,
                      controller=None, autoSetMacs=False)
        net.addController("c0", controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)
        net.start()
        if not net.waitConnected(timeout=20):
            raise RuntimeError("switches did not connect to the controller")
        start_servers(net, rundir)
        ok = wait_for_text(os.path.join(rundir, "controller.log"),
                           [f"[HEALTH] {s} is UP" for s in SERVERS], 25)
        if not ok:
            raise RuntimeError("controller did not see all three servers UP")
        time.sleep(1)
        ev = Events(rundir)
        RUNNERS[scenario](net, rundir, ev)
        ev.close()
        return True
    except Exception as e:                  # keep going with the next run
        log(f"    RUN FAILED: {e}")
        with open(os.path.join(rundir, "FAILED.txt"), "w") as f:
            f.write(str(e) + "\n")
        return False
    finally:
        if net is not None:
            net.stop()
        stop_controller(ctrl, ctrl_log)
        cleanup()


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
def summarise(batch_dir, runs):
    rows = []
    for scenario, label, lb, pp, rep, rundir in runs:
        for fname in sorted(os.listdir(rundir)):
            if not (fname.startswith("h") and fname.endswith(".csv")):
                continue
            with open(os.path.join(rundir, fname)) as f:
                for i, r in enumerate(csv.DictReader(f), 1):
                    r.update(scenario=scenario, config=label, lb_policy=lb, path_policy=pp,
                             rep=rep, request=i)
                    rows.append(r)

    if not rows:
        log("no client results found")
        return
    fields = ["scenario", "config", "lb_policy", "path_policy", "rep", "client", "request",
              "start_epoch", "served_by", "status", "attempts", "total_ms", "time_to_content_ms",
              "throughput_mbps", "bytes"]
    with open(os.path.join(batch_dir, "summary_requests.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["config"]), []).append(r)

    agg_fields = ["scenario", "config", "requests", "completed", "failed_requests",
                  "extra_attempts", "mean_throughput_mbps", "mean_time_to_content_ms",
                  "max_time_to_content_ms", "mean_response_ms"]
    agg_rows = []
    for (scenario, config), rs in groups.items():
        ok = [r for r in rs if r["status"] == "ok"]
        tput = [num(r["throughput_mbps"]) for r in ok if num(r["throughput_mbps"]) is not None]
        ttc = [num(r["time_to_content_ms"]) for r in ok if num(r["time_to_content_ms"]) is not None]
        resp = [num(r["total_ms"]) for r in ok if num(r["total_ms"]) is not None]
        extra = sum(max(0, int(num(r["attempts"]) or 1) - 1) for r in ok) + \
            sum(int(num(r["attempts"]) or 1) for r in rs if r["status"] != "ok")
        agg_rows.append({
            "scenario": scenario, "config": config, "requests": len(rs), "completed": len(ok),
            "failed_requests": len(rs) - len(ok), "extra_attempts": extra,
            "mean_throughput_mbps": round(sum(tput) / len(tput), 3) if tput else "",
            "mean_time_to_content_ms": round(sum(ttc) / len(ttc), 1) if ttc else "",
            "max_time_to_content_ms": round(max(ttc), 1) if ttc else "",
            "mean_response_ms": round(sum(resp) / len(resp), 1) if resp else "",
        })
    with open(os.path.join(batch_dir, "summary_by_config.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=agg_fields)
        w.writeheader()
        w.writerows(agg_rows)

    print()
    print(f"{'scenario':<11} {'config':<16} {'req':>4} {'ok':>4} {'fail':>5} {'retry':>6} "
          f"{'Mbps':>7} {'TTC ms':>9} {'max TTC':>9}")
    for a in agg_rows:
        print(f"{a['scenario']:<11} {a['config']:<16} {a['requests']:>4} {a['completed']:>4} "
              f"{a['failed_requests']:>5} {a['extra_attempts']:>6} "
              f"{str(a['mean_throughput_mbps']):>7} {str(a['mean_time_to_content_ms']):>9} "
              f"{str(a['max_time_to_content_ms']):>9}")
    print()


def give_back_to_user(path):
    """Results are written as root; hand them back to the user who ran sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None:
        return
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(name, int(uid), int(gid))
            except OSError:
                pass


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Run all SDN content-delivery experiments")
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    parser.add_argument("--reps", type=int, default=3, help="repetitions per configuration")
    parser.add_argument("--ryu", default=default_ryu(), help="path to ryu-manager")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo: sudo python3 tests/run_experiments.py")
    if not os.path.exists(args.ryu):
        sys.exit(f"ryu-manager not found at {args.ryu} (use --ryu PATH)")
    if not os.path.exists(os.path.join("app", "content", FILE)):
        sys.exit("Test files missing: run  python3 app/make_content.py  first")

    setLogLevel("warning")
    batch = os.path.join("results", "experiments", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(batch, exist_ok=True)
    plan = [(s, label, lb, pp, rep) for s in args.scenarios
            for (label, lb, pp) in SCENARIOS[s] for rep in range(1, args.reps + 1)]
    log(f"{len(plan)} runs planned; results in {batch}")

    done = []
    for n, (s, label, lb, pp, rep) in enumerate(plan, 1):
        rundir = os.path.join(batch, s, label, f"rep{rep}")
        log(f"run {n}/{len(plan)}: {s} / {label} / rep {rep}  ({DESCRIPTIONS[s]})")
        t = time.time()
        if run_once(args.ryu, s, label, lb, pp, rundir):
            done.append((s, label, lb, pp, rep, rundir))
        log(f"    finished in {time.time() - t:.0f} s")

    summarise(batch, done)
    give_back_to_user(os.path.join("results", "experiments"))
    log(f"done: {len(done)}/{len(plan)} runs succeeded; summary in {batch}/summary_by_config.csv")


if __name__ == "__main__":
    main()
