#!/usr/bin/env python3
"""
Demo dashboard for the SDN content-delivery project.

One command starts everything and serves a web page that both shows and drives the system:
the controller (Ryu), the Mininet network, the three content servers, and a control panel
for client requests, server failures, link failures, a congestion flood and policy changes.

Run from the project folder:
    sudo python3 dashboard/demo.py                      # real network, http://localhost:8080
    sudo python3 dashboard/demo.py --policy static      # start with another selection policy
    python3 dashboard/demo.py --sim                     # simulated network, no Mininet or sudo

Nothing else should be running first (it cleans up any old Mininet itself). The usual
mininet> prompt stays available in the terminal as a backup. Type exit (or press Ctrl+D)
to stop everything.

Each session's logs and CSVs are saved to results/demo/<date_time>/.
"""

import argparse
import csv
import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT, "controller"))
import net_config as cfg  # noqa: E402

VIP = cfg.VIP
CLIENTS = ["h1", "h2", "h3"]
SERVERS = ["srv1", "srv2", "srv3"]
FILES = {"small.bin": 10 * 1024, "medium.bin": 1024 * 1024, "large.bin": 5 * 1024 * 1024}
LB_POLICIES = ("static", "round_robin", "least_loaded")
PATH_POLICIES = ("delay", "congestion")
SWITCH_LINKS = [(a, b) for a, _pa, b, _pb, _d, _bw in cfg.LINKS]
DPID = {f"s{i}": i for i in (1, 2, 3, 4)}
UTIL_THRESHOLD = 0.7

SCENARIOS = {
    "concurrent": {
        "title": "Three clients at once",
        "text": "h1, h2 and h3 each download 3 × 5 MB at the same moment.",
        "show": "How the policy spreads clients over the servers. Compare static with least loaded.",
    },
    "serverfail": {
        "title": "Server crash",
        "text": "Three clients download 4 × 5 MB each; srv1 is killed at 6 s and restarted at 30 s.",
        "show": "Least loaded marks srv1 down within 3 s and avoids it; static keeps sending clients to it.",
    },
    "linkfail": {
        "title": "Link cut",
        "text": "h1 downloads 6 × 5 MB; link s1–s2 goes down at 6 s and comes back at 18 s.",
        "show": "Traffic moves to the slow path through s3 and back, with no failed downloads.",
    },
    "congestion": {
        "title": "Congested path",
        "text": "A 25 Mbps UDP flood fills the fast path; after 6 s h1 downloads 3 × 5 MB.",
        "show": "Congestion-aware routing sends h1 through s3. Compare with path policy delay.",
    },
}


def now():
    return time.time()


def num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def route_name(path):
    return "-".join(f"s{d}" for d in path)


def latest_results_batch():
    """Newest experiment batch with a summary, for the Results tab."""
    base = os.path.join(PROJECT, "results", "experiments")
    try:
        batches = sorted(d for d in os.listdir(base)
                         if os.path.exists(os.path.join(base, d, "summary_by_config.csv")))
    except OSError:
        return None
    if not batches:
        return None
    name = batches[-1]
    path = os.path.join(base, name)
    with open(os.path.join(path, "summary_by_config.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    reps = set()
    try:
        with open(os.path.join(path, "summary_requests.csv"), newline="") as f:
            for r in csv.DictReader(f):
                reps.add(r.get("rep"))
    except OSError:
        pass
    graphs = []
    gdir = os.path.join(path, "graphs")
    if os.path.isdir(gdir):
        graphs = [f"results/experiments/{name}/graphs/{g}"
                  for g in sorted(os.listdir(gdir)) if g.endswith(".png")]
    return {"batch": name, "rows": rows, "reps": len(reps) or None, "graphs": graphs}


# =============================================================================
# Shared state and scenario runner
# =============================================================================
class Backend:
    """What the web page talks to. LiveBackend and SimBackend fill in the details."""

    mode = "?"

    def __init__(self):
        self.lock = threading.RLock()
        self.started = now()
        self.chart_since = self.started
        self.events = deque(maxlen=400)
        self.util_history = deque(maxlen=400)
        self.decisions = deque(maxlen=40)
        self.scenario = None          # {"name", "t0", "step", "cancel"}
        self.lb_policy = "least_loaded"
        self.path_policy = "congestion"
        self.restarting = False

    # ---- events ------------------------------------------------------------
    def event(self, kind, text, mark=None):
        """kind: action | link | health | decision | ctrl | scenario | warn | error.
        mark: short label to draw on the charts' time axis (actions only)."""
        with self.lock:
            self.events.append({"t": now(), "kind": kind, "text": text, "mark": mark})

    # ---- actions (implemented by subclasses) ---------------------------------
    def request(self, client, file, count): raise NotImplementedError
    def server(self, name, up): raise NotImplementedError
    def link(self, a, b, up): raise NotImplementedError
    def flood(self, on, mbps=25): raise NotImplementedError
    def set_policy(self, lb, path): raise NotImplementedError
    def clients_busy(self): raise NotImplementedError

    def reset(self):
        """Put the network back to normal: links up, servers running, flood off."""
        self.stop_scenario()
        self.flood(False)
        for a, b in SWITCH_LINKS:
            self.link(a, b, True, quiet=True)
        for s in SERVERS:
            self.server(s, True, quiet=True)
        self.event("action", "Network reset: all links up, all servers running, flood off", "reset")

    def clear_charts(self):
        with self.lock:
            self.chart_since = now()

    # ---- scenarios -----------------------------------------------------------
    def start_scenario(self, name):
        if name not in SCENARIOS:
            raise ValueError("unknown scenario")
        if self.scenario and not self.scenario["done"]:
            raise RuntimeError("a scenario is already running; stop it first")
        if self.restarting:
            raise RuntimeError("the controller is restarting; try again in a few seconds")
        sc = {"name": name, "t0": now(), "step": "starting", "cancel": threading.Event(),
              "done": False}
        self.scenario = sc
        self.clear_charts()
        threading.Thread(target=self._run_scenario, args=(sc,), daemon=True).start()

    def stop_scenario(self):
        sc = self.scenario
        if sc and not sc["done"]:
            sc["cancel"].set()

    def _run_scenario(self, sc):
        name, t0, cancel = sc["name"], sc["t0"], sc["cancel"]
        title = SCENARIOS[name]["title"]
        self.event("scenario", f"Scenario started: {title} "
                               f"(policy {self.lb_policy}, path {self.path_policy})", title)

        def at(seconds):
            while not cancel.is_set() and now() < t0 + seconds:
                time.sleep(0.1)
            return not cancel.is_set()

        def step(text):
            sc["step"] = text

        try:
            if name == "concurrent":
                step("3 clients downloading")
                for h in CLIENTS:
                    self.request(h, "large.bin", 3, quiet=True)
                self.event("action", "h1, h2, h3: 3 × large.bin each, all at once")
            elif name == "serverfail":
                step("3 clients downloading")
                for h in CLIENTS:
                    self.request(h, "large.bin", 4, quiet=True)
                self.event("action", "h1, h2, h3: 4 × large.bin each")
                step("waiting to kill srv1 at 6 s")
                if at(6):
                    self.server("srv1", False)
                    step("srv1 killed; restart at 30 s")
                if at(30):
                    self.server("srv1", True)
                    step("srv1 restarted")
            elif name == "linkfail":
                step("h1 downloading")
                self.request("h1", "large.bin", 6, quiet=True)
                self.event("action", "h1: 6 × large.bin")
                step("cutting s1–s2 at 6 s")
                if at(6):
                    self.link(1, 2, False)
                    step("s1–s2 down; restoring at 18 s")
                if at(18):
                    self.link(1, 2, True)
                    step("s1–s2 restored")
            elif name == "congestion":
                step("flood starting")
                self.flood(True, 25)
                step("flood running; h1 starts at 6 s")
                if at(6):
                    self.request("h1", "large.bin", 3, quiet=True)
                    self.event("action", "h1: 3 × large.bin", "h1 starts")
                    step("h1 downloading through the flood")
            step("waiting for clients to finish")
            deadline = now() + 240
            while not cancel.is_set() and now() < deadline:
                time.sleep(0.5)
                if not self.clients_busy():
                    break
            if name == "congestion":
                self.flood(False)
            if cancel.is_set():
                self.event("scenario", f"Scenario stopped: {title}")
            else:
                self.event("scenario", f"Scenario finished: {title} "
                                       f"({now() - t0:.0f} s)", "end")
        except Exception as e:  # report and stop, never kill the server
            self.event("error", f"Scenario failed: {e}")
        finally:
            sc["done"] = True
            sc["step"] = "finished"

    # ---- state for the page --------------------------------------------------
    def base_state(self):
        sc = self.scenario
        with self.lock:
            since = self.chart_since
            return {
                "mode": self.mode, "now": now(), "started": self.started, "chart_since": since,
                "vip": VIP, "lb_policy": self.lb_policy, "path_policy": self.path_policy,
                "restarting": self.restarting, "util_threshold": UTIL_THRESHOLD,
                "events": list(self.events)[-120:],
                "util_history": [u for u in self.util_history if u["t"] >= since - 2],
                "decisions": list(self.decisions)[-15:][::-1],
                "scenario": None if sc is None else {
                    "name": sc["name"], "title": SCENARIOS[sc["name"]]["title"],
                    "elapsed": round(now() - sc["t0"], 1), "step": sc["step"], "done": sc["done"]},
                "scenarios": SCENARIOS,
            }


def summarize(rows):
    ok = [r for r in rows if r["status"] == "ok"]
    ttc = [r["ttc_ms"] for r in ok if r["ttc_ms"] is not None]
    mbps = [r["mbps"] for r in ok if r["mbps"] is not None]
    return {
        "total": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "retries": sum(max(0, (r["attempts"] or 1) - 1) for r in rows),
        "avg_ttc_ms": round(sum(ttc) / len(ttc)) if ttc else None,
        "max_ttc_ms": round(max(ttc)) if ttc else None,
        "avg_mbps": round(sum(mbps) / len(mbps), 2) if mbps else None,
    }


# =============================================================================
# Live backend: real Ryu controller + Mininet
# =============================================================================
TAG_KIND = {"[LINK]": "link", "[HEALTH]": "health", "[LB]": "decision", "[ROUTE]": "warn",
            "[SWITCH]": "ctrl", "[START]": "ctrl"}


class LiveBackend(Backend):
    mode = "live"

    def __init__(self, args):
        super().__init__()
        from mininet.net import Mininet
        from mininet.node import RemoteController, OVSSwitch
        from mininet.link import TCLink
        from mininet.clean import cleanup
        sys.path.insert(0, os.path.join(PROJECT, "topology"))
        from topo import ContentTopo, CONTROLLER_IP, CONTROLLER_PORT

        self._cleanup = cleanup
        self.ryu = args.ryu
        self.lb_policy, self.path_policy = args.policy, args.path_policy
        self.session = os.path.join(PROJECT, "results", "demo", time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(self.session, exist_ok=True)
        self.state_json = os.path.join(self.session, "controller_state.json")
        self.decisions_csv = os.path.join(self.session, "decisions.csv")
        self.ctrl = None
        self.ctrl_log = None
        self.ctrl_log_pos = 0
        self.ctrl_n = 0
        self.dec_pos = 0
        self.server_procs = {}
        self.client_jobs = []        # {"client", "proc", "count", "start_rows"}
        self.flood_proc = None
        self.sink_proc = None
        self.flood_mbps = 0
        self.links_down = set()

        print("Cleaning up any old Mininet ...")
        cleanup()
        self.start_controller()
        print("Starting the network ...")
        self.net = Mininet(topo=ContentTopo(), switch=OVSSwitch, link=TCLink,
                           controller=None, autoSetMacs=False)
        self.net.addController("c0", controller=RemoteController,
                               ip=CONTROLLER_IP, port=CONTROLLER_PORT)
        self.net.start()
        if not self.net.waitConnected(timeout=20):
            print("WARNING: not all switches connected to the controller yet")
        for s in SERVERS:
            self.server(s, True, quiet=True)
        if self.wait_log([f"[HEALTH] {s} is UP" for s in SERVERS], 25):
            self.event("ctrl", "All three servers are UP")
        else:
            self.event("warn", "Not all servers reported UP within 25 s; check srv*.log")
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---- controller --------------------------------------------------------
    def start_controller(self):
        self.ctrl_n += 1
        path = os.path.join(self.session, f"controller_{self.ctrl_n}.log")
        env = dict(os.environ, LB_POLICY=self.lb_policy, PATH_POLICY=self.path_policy,
                   LB_DECISIONS_CSV=self.decisions_csv, LB_STATE_JSON=self.state_json)
        self.ctrl_log = path
        self.ctrl_log_pos = 0
        logf = open(path, "w")
        print(f"Starting controller: LB_POLICY={self.lb_policy} PATH_POLICY={self.path_policy}")
        self.ctrl = subprocess.Popen([self.ryu, "controller/lb_controller.py"], cwd=PROJECT,
                                     stdout=logf, stderr=subprocess.STDOUT, env=env,
                                     start_new_session=True)
        if not self.wait_log(["[START]"], 25):
            raise RuntimeError(f"controller did not start; see {path}")

    def stop_controller(self):
        p = self.ctrl
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def wait_log(self, needles, timeout):
        deadline = now() + timeout
        while now() < deadline:
            try:
                text = open(self.ctrl_log).read()
                if all(n in text for n in needles):
                    return True
            except OSError:
                pass
            if self.ctrl and self.ctrl.poll() is not None:
                return False
            time.sleep(0.3)
        return False

    def set_policy(self, lb, path):
        if lb not in LB_POLICIES or path not in PATH_POLICIES:
            raise ValueError("unknown policy")
        if self.restarting:
            raise RuntimeError("already restarting")
        if self.clients_busy():
            raise RuntimeError("wait for the running downloads to finish (or reset) first")
        self.restarting = True

        def work():
            try:
                self.event("action", f"Restarting controller with policy {lb}, path {path}",
                           f"{lb}/{path}")
                self.stop_controller()
                for sw in ("s1", "s2", "s3", "s4"):   # drop the old controller's rules
                    try:
                        subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "del-flows", sw],
                                       capture_output=True, timeout=10)
                    except (OSError, subprocess.TimeoutExpired) as e:
                        self.event("warn", f"could not clear rules on {sw}: {e}")
                self.lb_policy, self.path_policy = lb, path
                try:
                    os.remove(self.state_json)
                except OSError:
                    pass
                self.start_controller()
                ok = self.wait_log([f"[SWITCH] s{i} connected" for i in (1, 2, 3, 4)], 30)
                ok = ok and self.wait_log([f"[HEALTH] {s} is UP" for s in SERVERS
                                           if self.server_running(s)], 15)
                self.event("ctrl", "Controller ready" if ok else
                           "Controller restarted, but not every switch/server reconnected yet")
            except Exception as e:
                self.event("error", f"Controller restart failed: {e}")
            finally:
                self.restarting = False
        threading.Thread(target=work, daemon=True).start()

    # ---- hosts ---------------------------------------------------------------
    def server_running(self, name):
        p = self.server_procs.get(name)
        return p is not None and p.poll() is None

    def server(self, name, up, quiet=False):
        if name not in SERVERS:
            raise ValueError("unknown server")
        with self.lock:
            if up:
                if self.server_running(name):
                    return
                out = open(os.path.join(self.session, f"{name}.log"), "a")
                self.server_procs[name] = self.net.get(name).popen(
                    ["python3", "app/server.py", f"--name={name}"], cwd=PROJECT,
                    stdout=out, stderr=subprocess.STDOUT)
                if not quiet:
                    self.event("action", f"{name} process started", f"{name} up")
            else:
                subprocess.run(["pkill", "-f", f"server.py --name={name}"], capture_output=True)
                p = self.server_procs.get(name)
                if p:
                    try:
                        p.wait(3)
                    except subprocess.TimeoutExpired:
                        p.kill()
                if not quiet:
                    self.event("action", f"{name} process killed", f"{name} killed")

    def request(self, client, file, count, quiet=False):
        if file not in FILES or not 1 <= int(count) <= 50:
            raise ValueError("bad file or count")
        targets = CLIENTS if client == "all" else [client]
        if any(c not in CLIENTS for c in targets):
            raise ValueError("unknown client")
        if self.restarting:
            raise RuntimeError("the controller is restarting")
        with self.lock:
            for c in targets:
                csv_path = os.path.join(self.session, f"{c}.csv")
                out = open(os.path.join(self.session, f"{c}_out.txt"), "a")
                proc = self.net.get(c).popen(
                    ["python3", "app/client.py", f"--name={c}", "--server", VIP, "--file", file,
                     "--count", str(int(count)), "--interval", "0", "--timeout", "10",
                     "--csv", csv_path], cwd=PROJECT, stdout=out, stderr=subprocess.STDOUT)
                self.client_jobs.append({"client": c, "proc": proc, "count": int(count),
                                         "start_rows": self._count_rows(csv_path), "file": file})
        if not quiet:
            who = "h1, h2, h3" if client == "all" else client
            self.event("action", f"{who}: {count} × {file} to {VIP}", f"{client} ×{count}")

    @staticmethod
    def _count_rows(path):
        try:
            with open(path) as f:
                return max(0, sum(1 for _ in f) - 1)
        except OSError:
            return 0

    def clients_busy(self):
        return any(j["proc"].poll() is None for j in self.client_jobs)

    def link(self, a, b, up, quiet=False):
        a, b = int(a), int(b)
        if (a, b) not in SWITCH_LINKS:
            raise ValueError("unknown link")
        with self.lock:
            if up == ((a, b) not in self.links_down):
                return
            self.net.configLinkStatus(f"s{a}", f"s{b}", "up" if up else "down")
            (self.links_down.discard if up else self.links_down.add)((a, b))
        if not quiet:
            self.event("action", f"Link s{a}–s{b} set {'up' if up else 'down'}",
                       f"s{a}–s{b} {'up' if up else 'down'}")

    def flood(self, on, mbps=25):
        with self.lock:
            running = self.flood_proc is not None and self.flood_proc.poll() is None
            if on and not running:
                mbps = max(1, min(100, int(mbps)))
                if self.sink_proc is None or self.sink_proc.poll() is not None:
                    self.sink_proc = self.net.get("sink").popen(
                        ["iperf", "-s", "-u"], stdout=open(os.path.join(self.session, "iperf_sink.txt"), "a"),
                        stderr=subprocess.STDOUT)
                    time.sleep(0.5)
                self.flood_proc = self.net.get("gen").popen(
                    ["iperf", "-c", "10.0.0.21", "-u", "-b", f"{mbps}M", "-t", "3600"],
                    stdout=open(os.path.join(self.session, "flood.txt"), "a"), stderr=subprocess.STDOUT)
                self.flood_mbps = mbps
                self.event("action", f"UDP flood started: gen → sink, {mbps} Mbps", f"flood {mbps}M")
            elif not on and running:
                self.flood_proc.kill()
                self.flood_proc.wait()
                self.event("action", "UDP flood stopped", "flood off")

    # ---- polling -------------------------------------------------------------
    def _poll_loop(self):
        while True:
            try:
                self._tail_controller_log()
                self._tail_decisions()
                st = self._read_state()
                if st:
                    with self.lock:
                        if not self.util_history or st["time"] > self.util_history[-1]["t"]:
                            self.util_history.append({"t": st["time"],
                                                      "u": [l["util"] if l["up"] else None
                                                            for l in st["links"]]})
            except Exception as e:
                self.event("error", f"dashboard poll error: {e}")
            time.sleep(0.5)

    def _tail_controller_log(self):
        try:
            with open(self.ctrl_log) as f:
                f.seek(self.ctrl_log_pos)
                chunk = f.read()
                self.ctrl_log_pos = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            line = line.strip()
            tag = next((t for t in TAG_KIND if t in line), None)
            if tag == "[LB]":
                if "rejected" in line:   # normal decisions come from decisions.csv instead
                    self.event("warn", line[line.index(tag):])
            elif tag:
                self.event(TAG_KIND[tag], line[line.index(tag):])
            elif "Traceback" in line or "Error" in line:
                self.event("error", "controller: " + line[:200])

    def _tail_decisions(self):
        try:
            with open(self.decisions_csv, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            return
        new = rows[self.dec_pos:]
        self.dec_pos = len(rows)
        for r in new:
            d = {"t": now(), "client": r.get("client"), "port": r.get("client_port"),
                 "server": r.get("server"), "path": r.get("path"), "policy": r.get("policy"),
                 "path_policy": r.get("path_policy"), "loads": r.get("loads"),
                 "link_util": r.get("link_util")}
            with self.lock:
                self.decisions.append(d)
            self.event("decision", f"{d['client']} → {d['server']} via {d['path']}")

    def _read_state(self):
        try:
            with open(self.state_json) as f:
                st = json.load(f)
            st["_age"] = now() - os.path.getmtime(self.state_json)
            return st
        except (OSError, ValueError):
            return None

    def _requests(self):
        rows = []
        for c in CLIENTS:
            try:
                with open(os.path.join(self.session, f"{c}.csv"), newline="") as f:
                    raw = list(csv.DictReader(f))
            except OSError:
                continue
            for r in raw:
                start = num(r.get("start_epoch"))
                ttc = num(r.get("time_to_content_ms"))
                if start is None:
                    continue
                rows.append({
                    "client": c, "start": start, "end": start + (ttc or 0) / 1000,
                    "served_by": r.get("served_by", "-"),
                    "status": "ok" if r.get("status") == "ok" else "failed",
                    "error": "" if r.get("status") == "ok" else r.get("status", ""),
                    "total_ms": num(r.get("total_ms")), "ttc_ms": ttc,
                    "mbps": num(r.get("throughput_mbps")),
                    "attempts": int(num(r.get("attempts"), 1)), "file": r.get("file")})
        rows.sort(key=lambda r: r["end"])
        return rows

    def state(self):
        out = self.base_state()
        st = self._read_state()
        ctrl_alive = self.ctrl is not None and self.ctrl.poll() is None
        out["controller_online"] = bool(st and ctrl_alive and st["_age"] < 4 and not self.restarting)
        out["session_dir"] = os.path.relpath(self.session, PROJECT)
        st = st or {}
        servers = {s["name"]: s for s in st.get("servers", [])}
        out["servers"] = []
        for name in SERVERS:
            ip = next(i for i, h in cfg.HOSTS.items() if h[0] == name)
            s = servers.get(name, {})
            out["servers"].append({
                "name": name, "ip": ip, "process": self.server_running(name),
                "alive": bool(s.get("alive")) and out["controller_online"],
                "active": s.get("active", 0) + s.get("pending", 0),
                "last_report_age": s.get("last_report_age"), "delay_ms": cfg.SERVER_DELAY_MS[ip]})
        links = {(l["a"], l["b"]): l for l in st.get("links", [])}
        out["links"] = []
        for a, pa, b, pb, d, bw in cfg.LINKS:
            l = links.get((a, b), {})
            out["links"].append({"a": a, "b": b, "delay_ms": d, "bw_mbps": bw,
                                 "up": (a, b) not in self.links_down,
                                 "controller_sees_up": l.get("up", True),
                                 "util": l.get("util", 0.0), "mbps": l.get("mbps", 0.0)})
        out["connections"] = st.get("connections", [])
        flood_on = self.flood_proc is not None and self.flood_proc.poll() is None
        out["flood"] = {"on": flood_on, "mbps": self.flood_mbps if flood_on else 0}
        rows = self._requests()
        out["requests"] = [r for r in rows if r["end"] >= out["chart_since"]]
        out["summary"] = summarize(out["requests"])
        out["clients"] = {}
        for c in CLIENTS:
            jobs = [j for j in self.client_jobs if j["client"] == c and j["proc"].poll() is None]
            if jobs:
                done = self._count_rows(os.path.join(self.session, f"{c}.csv"))
                total = sum(j["count"] for j in jobs)
                first = min(j["start_rows"] for j in jobs)
                out["clients"][c] = {"busy": True, "done": min(total, done - first), "total": total}
            else:
                out["clients"][c] = {"busy": False}
        return out

    def shutdown(self):
        print("\nStopping everything ...")
        for j in self.client_jobs:
            if j["proc"].poll() is None:
                j["proc"].kill()
        for p in (self.flood_proc, self.sink_proc):
            if p and p.poll() is None:
                p.kill()
        for s in SERVERS:
            subprocess.run(["pkill", "-f", f"server.py --name={s}"], capture_output=True)
        try:
            self.net.stop()
        except Exception:
            pass
        self.stop_controller()
        self._cleanup()
        give_back_to_user(os.path.join(PROJECT, "results", "demo"))
        print(f"Session saved in {os.path.relpath(self.session, PROJECT)}")


def give_back_to_user(path):
    """Files are written as root; hand them back to the user who ran sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None:
        return
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.chown(name, int(uid), int(gid))
            except OSError:
                pass


# =============================================================================
# Simulated backend: same page, same controls, no Mininet (for rehearsal)
# =============================================================================
class SimBackend(Backend):
    """A rough model of the real system, so the page can be practised anywhere.

    Numbers are illustrative, not measurements: use the live mode or the Results tab
    for real data.
    """

    mode = "sim"
    HOST_BW = 9.4      # Mbps achieved on a 10 Mbps host link in the real runs
    CORE_BW = 20.0

    def __init__(self, args):
        super().__init__()
        self.lb_policy, self.path_policy = args.policy, args.path_policy
        t = now()
        self.srv = {s: {"process": True, "alive": True, "last": t, "pending": 0} for s in SERVERS}
        self.link_up = {l: True for l in SWITCH_LINKS}
        self.util = {l: 0.0 for l in SWITCH_LINKS}
        self.util_measured = {l: 0.0 for l in SWITCH_LINKS}
        self.last_sample = t
        self.flood_mbps = 0
        self.jobs = []      # client batches
        self.dls = []       # active downloads
        self.rows = []      # finished requests
        self.rr = 0
        self.restart_until = 0
        self.event("ctrl", f"[START] policy={self.lb_policy}  path_policy={self.path_policy} (simulated)")
        for s in SERVERS:
            self.event("health", f"[HEALTH] {s} is UP (load reports received)")
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- actions -------------------------------------------------------------
    def request(self, client, file, count, quiet=False):
        if file not in FILES or not 1 <= int(count) <= 50:
            raise ValueError("bad file or count")
        targets = CLIENTS if client == "all" else [client]
        if any(c not in CLIENTS for c in targets):
            raise ValueError("unknown client")
        if self.restarting:
            raise RuntimeError("the controller is restarting")
        with self.lock:
            for c in targets:
                self.jobs.append({"client": c, "file": file, "count": int(count), "done": 0,
                                  "next_at": now(), "current": None})
        if not quiet:
            who = "h1, h2, h3" if client == "all" else client
            self.event("action", f"{who}: {count} × {file} to {VIP}", f"{client} ×{count}")

    def server(self, name, up, quiet=False):
        if name not in SERVERS:
            raise ValueError("unknown server")
        with self.lock:
            s = self.srv[name]
            if s["process"] == up:
                return
            s["process"] = up
            if not up:
                for d in self.dls:
                    if d["server"] == name:
                        self._fail(d, "connection reset by peer")
        if not quiet:
            self.event("action", f"{name} process {'started' if up else 'killed'}",
                       f"{name} {'up' if up else 'killed'}")

    def link(self, a, b, up, quiet=False):
        a, b = int(a), int(b)
        if (a, b) not in SWITCH_LINKS:
            raise ValueError("unknown link")
        with self.lock:
            if self.link_up[(a, b)] == up:
                return
            self.link_up[(a, b)] = up
        if not quiet:
            self.event("action", f"Link s{a}–s{b} set {'up' if up else 'down'}",
                       f"s{a}–s{b} {'up' if up else 'down'}")
        self.event("link", f"[LINK] s{a}-s{b} is {'UP' if up else 'DOWN'} -> clearing dynamic rules to re-route")
        with self.lock:   # the controller reinstalls every connection on its new best path
            for d in self.dls:
                p = self._path(1, 4, d["congestion_aware"])
                if p is None:
                    self._fail(d, "timed out")
                else:
                    d["path"] = p

    def flood(self, on, mbps=25):
        with self.lock:
            if on and not self.flood_mbps:
                self.flood_mbps = max(1, min(100, int(mbps)))
                self.event("action", f"UDP flood started: gen → sink, {self.flood_mbps} Mbps",
                           f"flood {self.flood_mbps}M")
            elif not on and self.flood_mbps:
                self.flood_mbps = 0
                self.event("action", "UDP flood stopped", "flood off")

    def set_policy(self, lb, path):
        if lb not in LB_POLICIES or path not in PATH_POLICIES:
            raise ValueError("unknown policy")
        if self.restarting:
            raise RuntimeError("already restarting")
        if self.clients_busy():
            raise RuntimeError("wait for the running downloads to finish (or reset) first")
        self.event("action", f"Restarting controller with policy {lb}, path {path}", f"{lb}/{path}")
        with self.lock:
            self.restarting = True
            self.restart_until = now() + 4
            self.lb_policy, self.path_policy = lb, path

    def clients_busy(self):
        return bool(self.jobs)

    # ---- model ---------------------------------------------------------------
    def _path(self, src, dst, congestion):
        dist, prev, todo = {src: 0}, {}, {src}
        seen = set()
        while todo:
            u = min(todo, key=lambda n: dist[n])
            todo.discard(u)
            seen.add(u)
            for (a, b), d in zip(SWITCH_LINKS, [l[4] for l in cfg.LINKS]):
                if not self.link_up[(a, b)] or u not in (a, b):
                    continue
                v = b if u == a else a
                cost = d + (100 if congestion and self.util_measured[(a, b)] >= UTIL_THRESHOLD else 0)
                if v not in seen and dist[u] + cost < dist.get(v, math.inf):
                    dist[v], prev[v] = dist[u] + cost, u
                    todo.add(v)
        if dst not in dist:
            return None
        p = [dst]
        while p[0] != src:
            p.insert(0, prev[p[0]])
        return p

    def _alive(self):
        t = now()
        return [s for s in SERVERS if t - self.srv[s]["last"] < 3]

    def _choose(self):
        if self.lb_policy == "static":
            return "srv1"
        alive = self._alive()
        if not alive:
            return None
        if self.lb_policy == "round_robin":
            s = alive[self.rr % len(alive)]
            self.rr += 1
            return s
        load = {s: sum(1 for d in self.dls if d["server"] == s) + self.srv[s]["pending"] for s in alive}
        return min(alive, key=lambda s: (load[s], cfg.SERVER_DELAY_MS[f"10.0.0.1{s[-1]}"]))

    def _loads(self):
        alive = self._alive()
        return " ".join(f"{s}=UP({sum(1 for d in self.dls if d['server'] == s)})" if s in alive
                        else f"{s}=DOWN" for s in SERVERS)

    def _util_text(self):
        return " ".join(f"s{a}-s{b}=" + (f"{round(self.util_measured[(a, b)] * 100)}%"
                                         if self.link_up[(a, b)] else "DOWN")
                        for a, b in SWITCH_LINKS)

    def _start_attempt(self, job):
        req = job["current"]
        req["attempts"] += 1
        server = self._choose()
        aware = self.path_policy == "congestion"
        path = self._path(1, 4, aware)
        port = random.randint(40000, 60999)
        if server is None or path is None:
            self.event("warn", f"[LB] {job['client']}:{port} rejected: no server is UP")
            self._attempt_failed(job, "timed out")
            return
        self.srv[server]["pending"] += 1
        self.decisions.append({"t": now(), "client": job["client"], "port": port, "server": server,
                               "path": route_name(path), "policy": self.lb_policy,
                               "path_policy": self.path_policy, "loads": self._loads(),
                               "link_util": self._util_text()})
        self.event("decision", f"{job['client']} → {server} via {route_name(path)}")
        if not self.srv[server]["process"]:
            self._attempt_failed(job, "connection refused" if self.lb_policy != "static"
                                 else "timed out")
            return
        self.dls.append({"job": job, "client": job["client"], "server": server, "path": path,
                         "port": port, "left": FILES[job["file"]] * 8 / 1e6, "rate": 0.0,
                         "begin": now(), "congestion_aware": aware, "starved": 0.0})

    def _fail(self, d, why):
        if d in self.dls:
            self.dls.remove(d)
        self._attempt_failed(d["job"], why)

    def _attempt_failed(self, job, why):
        req = job["current"]
        if req["attempts"] >= 3:
            self._finish(job, ok=False, why=why)
        else:
            req["retry_at"] = now() + 1.0 + (10 if why == "timed out" else 0)

    def _finish(self, job, ok, server=None, mbps=None, total_ms=None, why=""):
        req = job["current"]
        t = now()
        self.rows.append({"client": job["client"], "start": req["start"], "end": t,
                          "served_by": server or "-", "status": "ok" if ok else "failed",
                          "error": "" if ok else f"failed: {why}",
                          "total_ms": total_ms, "ttc_ms": round((t - req["start"]) * 1000, 1),
                          "mbps": mbps, "attempts": req["attempts"], "file": job["file"]})
        job["done"] += 1
        job["current"] = None
        job["next_at"] = t

    def _rates(self):
        """Share each bottleneck among the TCP downloads crossing it (the flood takes its share first)."""
        users = {}
        for d in self.dls:
            keys = [("srv", d["server"]), ("cli", d["client"])] + \
                   [("link", tuple(sorted(e))) for e in zip(d["path"], d["path"][1:])]
            d["keys"] = keys
            for k in keys:
                users[k] = users.get(k, 0) + 1
        flood_on = self._flood_links()
        for d in self.dls:
            caps = []
            for k in d["keys"]:
                if k[0] == "link":
                    free = self.CORE_BW - (self.flood_mbps if k[1] in flood_on else 0)
                    caps.append(max(0.25, free) / users[k])
                else:
                    caps.append(self.HOST_BW / users[k])
            d["rate"] = min(caps) * random.uniform(0.96, 1.02)

    def _flood_links(self):
        if not self.flood_mbps:
            return set()
        p = self._path(4, 1, False)       # background traffic always takes the shortest path
        return {tuple(sorted(e)) for e in zip(p, p[1:])} if p else set()

    def _loop(self):
        last = now()
        while True:
            time.sleep(0.2)
            t = now()
            dt = t - last
            last = t
            with self.lock:
                self._tick(t, dt)

    def _tick(self, t, dt):
        if self.restarting and t >= self.restart_until:
            self.restarting = False
            for s in SERVERS:
                self.srv[s]["alive"] = False
            self.event("ctrl", f"[START] policy={self.lb_policy}  path_policy={self.path_policy} (simulated)")
            self.event("ctrl", "Controller ready")
        # server reports and health
        for s, st in self.srv.items():
            if st["process"] and not self.restarting:
                st["last"] = t
                st["pending"] = 0
                if not st["alive"]:
                    st["alive"] = True
                    self.event("health", f"[HEALTH] {s} is UP (load reports received)")
            if st["alive"] and t - st["last"] >= 3:
                st["alive"] = False
                self.event("health", f"[HEALTH] {s} is DOWN (no report for 3s)")
        # client batches
        for job in list(self.jobs):
            if job["current"] is None:
                if job["done"] >= job["count"]:
                    self.jobs.remove(job)
                    continue
                if t >= job["next_at"] and not self.restarting:
                    job["current"] = {"start": t, "attempts": 0, "retry_at": t}
            req = job["current"]
            if req and req.get("retry_at") is not None and t >= req["retry_at"] and not self.restarting:
                req["retry_at"] = None
                self._start_attempt(job)
        # transfers
        self._rates()
        for d in list(self.dls):
            d["left"] -= d["rate"] * dt
            d["starved"] = d["starved"] + dt if d["rate"] < 1.0 else 0
            if d["starved"] > 6 and random.random() < 0.25 * dt:
                self._fail(d, "timed out")
                continue
            if d["left"] <= 0:
                self.dls.remove(d)
                el = t - d["begin"]
                size = FILES[d["job"]["file"]]
                self._finish(d["job"], ok=True, server=d["server"], total_ms=round(el * 1000, 1),
                             mbps=round(size * 8 / el / 1e6, 3))
        # link use (what the switches' counters would show) and the controller's 2 s samples
        flood = self._flood_links()
        for l in SWITCH_LINKS:
            key = tuple(sorted(l))
            if not self.link_up[l]:
                self.util[l] = 0.0
                continue
            mbps = sum(d["rate"] for d in self.dls if key in [tuple(sorted(e)) for e in zip(d["path"], d["path"][1:])])
            mbps += min(self.CORE_BW, self.flood_mbps) if key in flood else 0
            self.util[l] = min(1.0, mbps / self.CORE_BW)
        if t - self.last_sample >= 2:
            self.last_sample = t
            self.util_measured = dict(self.util)
        if not self.util_history or t - self.util_history[-1]["t"] >= 1:
            self.util_history.append({"t": t, "u": [self.util[l] if self.link_up[l] else None
                                                    for l in SWITCH_LINKS]})

    def state(self):
        out = self.base_state()
        with self.lock:
            t = now()
            out["controller_online"] = not self.restarting
            out["session_dir"] = "(simulated, nothing saved)"
            out["servers"] = []
            for name in SERVERS:
                ip = f"10.0.0.1{name[-1]}"
                st = self.srv[name]
                out["servers"].append({
                    "name": name, "ip": ip, "process": st["process"], "alive": st["alive"],
                    "active": sum(1 for d in self.dls if d["server"] == name),
                    "last_report_age": round(t - st["last"], 1), "delay_ms": cfg.SERVER_DELAY_MS[ip]})
            out["links"] = [{"a": a, "b": b, "delay_ms": d, "bw_mbps": bw, "up": self.link_up[(a, b)],
                             "controller_sees_up": self.link_up[(a, b)],
                             "util": round(self.util_measured[(a, b)], 3),
                             "mbps": round(self.util[(a, b)] * bw, 2)}
                            for a, _pa, b, _pb, d, bw in cfg.LINKS]
            out["connections"] = [{"client": d["client"], "port": d["port"], "server": d["server"],
                                   "path": d["path"], "age": round(t - d["begin"], 1)} for d in self.dls]
            out["flood"] = {"on": bool(self.flood_mbps), "mbps": self.flood_mbps}
            out["requests"] = [r for r in self.rows if r["end"] >= out["chart_since"]]
            out["summary"] = summarize(out["requests"])
            out["clients"] = {}
            for c in CLIENTS:
                jobs = [j for j in self.jobs if j["client"] == c]
                out["clients"][c] = ({"busy": True, "done": sum(j["done"] for j in jobs),
                                      "total": sum(j["count"] for j in jobs)} if jobs else {"busy": False})
        return out

    def shutdown(self):
        pass


# =============================================================================
# Web server
# =============================================================================
def make_handler(backend):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self.send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.send_json(backend.state())
            elif path == "/api/results":
                self.send_json(latest_results_batch() or {})
            elif path.startswith("/results/experiments/") and path.endswith(".png"):
                full = os.path.realpath(os.path.join(PROJECT, path.lstrip("/")))
                if full.startswith(os.path.join(PROJECT, "results", "experiments") + os.sep):
                    self.send_file(full, "image/png")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/api/action":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(length, 10000)) or b"{}")
                msg = run_action(backend, body)
                self.send_json({"ok": True, "message": msg})
            except (ValueError, RuntimeError, KeyError, TypeError) as e:
                self.send_json({"ok": False, "message": str(e)}, 400)
            except Exception as e:
                backend.event("error", f"action failed: {e}")
                self.send_json({"ok": False, "message": f"failed: {e}"}, 500)

        def send_json(self, data, code=200):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def run_action(b, body):
    a = body.get("action")
    if a == "request":
        b.request(body.get("client", "h1"), body.get("file", "medium.bin"), int(body.get("count", 3)))
        return "Requests sent"
    if a == "server":
        b.server(body["name"], body["op"] == "start")
        return f"{body['name']} {'started' if body['op'] == 'start' else 'killed'}"
    if a == "link":
        x, y = DPID[body["a"]], DPID[body["b"]]
        b.link(x, y, body["op"] == "up")
        return f"Link {body['a']}–{body['b']} {body['op']}"
    if a == "flood":
        b.flood(body["op"] == "start", int(body.get("mbps", 25)))
        return "Flood " + ("started" if body["op"] == "start" else "stopped")
    if a == "policy":
        b.set_policy(body["lb"], body["path"])
        return "Restarting the controller"
    if a == "scenario":
        b.start_scenario(body["name"])
        return "Scenario started"
    if a == "scenario_stop":
        b.stop_scenario()
        return "Stopping scenario"
    if a == "reset":
        b.reset()
        return "Network reset"
    if a == "clear":
        b.clear_charts()
        return "Charts cleared"
    raise ValueError(f"unknown action {a!r}")


def default_ryu():
    import pwd
    user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, "ryu-env", "bin", "ryu-manager")


def main():
    ap = argparse.ArgumentParser(description="SDN content-delivery demo dashboard")
    ap.add_argument("--sim", action="store_true", help="simulated network (no Mininet, no sudo)")
    ap.add_argument("--policy", choices=LB_POLICIES, default="least_loaded")
    ap.add_argument("--path-policy", choices=PATH_POLICIES, default="congestion")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address for the web page; 0.0.0.0 to open it from another computer")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ryu", default=None, help="path to ryu-manager (default ~/ryu-env/bin/ryu-manager)")
    ap.add_argument("--no-cli", action="store_true", help="don't open the mininet> prompt")
    args = ap.parse_args()
    os.chdir(PROJECT)

    if args.sim:
        backend = SimBackend(args)
    else:
        if os.geteuid() != 0:
            sys.exit("The real network needs sudo:  sudo python3 dashboard/demo.py\n"
                     "(or try the page without Mininet:  python3 dashboard/demo.py --sim)")
        args.ryu = args.ryu or default_ryu()
        if not os.path.exists(args.ryu):
            sys.exit(f"ryu-manager not found at {args.ryu} (use --ryu PATH)")
        if not os.path.exists(os.path.join("app", "content", "large.bin")):
            sys.exit("Test files missing: run  python3 app/make_content.py  first")
        from mininet.log import setLogLevel
        setLogLevel("warning")
        backend = LiveBackend(args)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(backend))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"\n  Dashboard ({backend.mode}): http://{shown}:{args.port}\n")

    try:
        if backend.mode == "live" and not args.no_cli and sys.stdin.isatty():
            from mininet.cli import CLI
            print("  The mininet> prompt below still works. Type exit to stop everything.\n")
            CLI(backend.net)
        else:
            print("  Press Ctrl+C to stop.\n")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        backend.shutdown()


if __name__ == "__main__":
    main()
