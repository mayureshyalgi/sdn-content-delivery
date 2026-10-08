#!/usr/bin/env python3
"""
Turn an experiment batch into graphs for the report.

    python3 tests/make_graphs.py                     # newest batch in results/experiments/
    python3 tests/make_graphs.py results/experiments/20261008_002750

Reads summary_requests.csv plus each run's events.csv and client CSVs, and writes PNGs
into <batch>/graphs/. Error bars show the standard deviation across repetitions.
No sudo needed.
"""

import csv
import glob
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"static": "#B23A48", "round_robin": "#E09F3E", "least_loaded": "#2E7D5B",
          "path_delay": "#B23A48", "path_congestion": "#2E7D5B"}
LABELS = {"static": "Static", "round_robin": "Round-robin", "least_loaded": "Least-loaded",
          "path_delay": "Congestion-blind", "path_congestion": "Congestion-aware"}
CLIENT_COLORS = {"h1": "#2B6CB0", "h2": "#E09F3E", "h3": "#6B46C1"}

plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.3,
                     "axes.axisbelow": True})


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(batch):
    with open(os.path.join(batch, "summary_requests.csv")) as f:
        return list(csv.DictReader(f))


def per_rep_stat(rows, scenario, config, field, ok_only=True):
    """Mean of `field` within each repetition, then mean and std across repetitions."""
    reps = {}
    for r in rows:
        if r["scenario"] != scenario or r["config"] != config:
            continue
        if ok_only and r["status"] != "ok":
            continue
        v = num(r[field])
        if v is not None:
            reps.setdefault(r["rep"], []).append(v)
    means = [statistics.mean(v) for v in reps.values() if v]
    if not means:
        return None, None
    return statistics.mean(means), (statistics.stdev(means) if len(means) > 1 else 0.0)


def configs_for(rows, scenario, order):
    present = {r["config"] for r in rows if r["scenario"] == scenario}
    return [c for c in order if c in present]


def bar_pair(ax, rows, scenario, configs, field, ylabel, title, scale=1.0, fmt="{:.1f}"):
    means, errs = [], []
    for c in configs:
        m, s = per_rep_stat(rows, scenario, c, field)
        means.append((m or 0) * scale)
        errs.append((s or 0) * scale)
    x = range(len(configs))
    bars = ax.bar(x, means, yerr=errs, capsize=5, color=[COLORS[c] for c in configs], width=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABELS[c] for c in configs])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    for b, m, e in zip(bars, means, errs):
        ax.annotate(fmt.format(m), (b.get_x() + b.get_width() / 2, m + e),
                    xytext=(0, 4), textcoords="offset points", ha="center", fontsize=9)


def save(fig, out, name):
    path = os.path.join(out, name)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print("wrote", path)


# ---------------------------------------------------------------------------
def graph_concurrent(rows, out):
    configs = configs_for(rows, "concurrent", ["static", "round_robin", "least_loaded"])
    if not configs:
        return
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 4))
    bar_pair(a, rows, "concurrent", configs, "throughput_mbps", "Mbps per client",
             "Throughput per client", fmt="{:.2f}")
    bar_pair(b, rows, "concurrent", configs, "time_to_content_ms", "seconds",
             "Time to content (5 MB)", scale=0.001)
    fig.suptitle("Three clients starting together: static vs dynamic server selection")
    save(fig, out, "1_concurrent_policies.png")


def graph_congestion(rows, out):
    configs = configs_for(rows, "congestion", ["path_delay", "path_congestion"])
    if not configs:
        return
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 4))
    bar_pair(a, rows, "congestion", configs, "time_to_content_ms", "seconds",
             "Mean time to content", scale=0.001)
    # worst case and wasted attempts
    worst, wasted = [], []
    for c in configs:
        ttc = [num(r["time_to_content_ms"]) for r in rows
               if r["scenario"] == "congestion" and r["config"] == c and r["status"] == "ok"]
        worst.append(max([t for t in ttc if t is not None] or [0]) / 1000)
        wasted.append(sum(max(0, int(num(r["attempts"]) or 1) - 1) for r in rows
                          if r["scenario"] == "congestion" and r["config"] == c))
    x = range(len(configs))
    bars = b.bar(x, worst, color=[COLORS[c] for c in configs], width=0.6)
    b.set_xticks(list(x))
    b.set_xticklabels([LABELS[c] for c in configs])
    b.set_ylabel("seconds")
    b.set_title("Worst-case time to content")
    for bar, w, n in zip(bars, worst, wasted):
        b.annotate(f"{w:.1f} s\n{n} failed attempts", (bar.get_x() + bar.get_width() / 2, w),
                   xytext=(0, 4), textcoords="offset points", ha="center", fontsize=9)
    fig.suptitle("25 Mbps flood on the fast path: congestion-blind vs congestion-aware routing")
    save(fig, out, "2_congestion_routing.png")


def graph_serverfail(rows, out):
    configs = configs_for(rows, "serverfail", ["static", "least_loaded"])
    if not configs:
        return
    fig, ax = plt.subplots(figsize=(6.5, 4))
    ok = [sum(1 for r in rows if r["scenario"] == "serverfail" and r["config"] == c
              and r["status"] == "ok") for c in configs]
    bad = [sum(1 for r in rows if r["scenario"] == "serverfail" and r["config"] == c
               and r["status"] != "ok") for c in configs]
    x = list(range(len(configs)))
    ax.bar(x, ok, color="#2E7D5B", width=0.6, label="completed")
    ax.bar(x, bad, bottom=ok, color="#B23A48", width=0.6, label="failed")
    ax.set_xticks(x)
    ax.set_xticklabels([LABELS[c] for c in configs])
    ax.set_ylabel("requests")
    ax.set_title("srv1 killed during downloads: requests completed vs failed")
    for i, (o, f) in enumerate(zip(ok, bad)):
        ax.annotate(f"{o}/{o + f}", (i, o + f), xytext=(0, 4), textcoords="offset points",
                    ha="center")
    ax.legend(loc="upper right")
    save(fig, out, "3_server_failure.png")


def run_timeline(batch, scenario, config, rep, title, name, out):
    """Each download drawn as a bar from start to finish; injected events as vertical lines."""
    rundir = os.path.join(batch, scenario, config, f"rep{rep}")
    ev_path = os.path.join(rundir, "events.csv")
    if not os.path.exists(ev_path):
        return
    with open(ev_path) as f:
        events = list(csv.DictReader(f))
    t0 = num(events[0]["epoch"]) - num(events[0]["t_s"])
    fig, ax = plt.subplots(figsize=(10, 3.6))
    clients = sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(rundir, "h*.csv")))
    for row_i, c in enumerate(clients):
        with open(os.path.join(rundir, f"{c}.csv")) as f:
            for r in csv.DictReader(f):
                start = num(r.get("start_epoch"))
                ttc = num(r.get("time_to_content_ms"))
                if start is None or ttc is None:
                    continue
                okay = r["status"] == "ok"
                ax.barh(row_i, ttc / 1000, left=start - t0, height=0.55,
                        color=CLIENT_COLORS.get(c, "#555") if okay else "#B23A48",
                        edgecolor="white", alpha=0.9 if okay else 0.6)
                mbps = num(r.get("throughput_mbps"))
                if not okay:
                    label = "failed"
                elif mbps is None:
                    label = r["served_by"]
                else:
                    label = f"{r['served_by']} {mbps:.1f}"
                tries = int(num(r.get("attempts")) or 1)
                if okay and tries > 1:
                    label += f" ({tries} tries)"
                    ax.barh(row_i, ttc / 1000, left=start - t0, height=0.55, fill=False,
                            hatch="//", edgecolor="white", lw=0)
                ax.text(start - t0 + ttc / 2000, row_i, label, ha="center", va="center",
                        fontsize=7, color="white")
    for e in events:
        if any(k in e["event"] for k in ("down", "up", "killed", "flood")):
            t = num(e["t_s"])
            ax.axvline(t, color="black", ls="--", lw=1)
            ax.text(t, len(clients) - 0.45, " " + e["event"], fontsize=8, va="bottom")
    ax.set_yticks(range(len(clients)))
    ax.set_yticklabels(clients)
    ax.set_xlabel("seconds since run start (bar = one download; label = server, Mbps)")
    ax.set_ylim(-0.6, len(clients) + 0.2)
    ax.set_title(title)
    ax.grid(axis="y", visible=False)
    save(fig, out, name)


def graph_summary_table(batch, out):
    path = os.path.join(batch, "summary_by_config.csv")
    with open(path) as f:
        rows = list(csv.DictReader(f))
    cols = ["scenario", "config", "requests", "completed", "failed_requests", "extra_attempts",
            "mean_throughput_mbps", "mean_time_to_content_ms", "max_time_to_content_ms"]
    heads = ["Scenario", "Config", "Req", "OK", "Failed", "Retries", "Mbps", "TTC ms", "Max TTC"]
    fig, ax = plt.subplots(figsize=(11, 0.45 * len(rows) + 1))
    ax.axis("off")
    t = ax.table(cellText=[[r[c] for c in cols] for r in rows], colLabels=heads,
                 loc="center", cellLoc="center")
    t.auto_set_font_size(False)
    t.set_fontsize(9)
    t.scale(1, 1.4)
    save(fig, out, "0_summary_table.png")


def main():
    if len(sys.argv) > 1:
        batch = sys.argv[1]
    else:
        batches = sorted(glob.glob(os.path.join("results", "experiments", "2*")))
        if not batches:
            sys.exit("no batches found in results/experiments/")
        batch = batches[-1]
    out = os.path.join(batch, "graphs")
    os.makedirs(out, exist_ok=True)
    print("batch:", batch)
    rows = load_rows(batch)
    graph_summary_table(batch, out)
    graph_concurrent(rows, out)
    graph_congestion(rows, out)
    graph_serverfail(rows, out)
    run_timeline(batch, "linkfail", "least_loaded", 1,
                 "Link s1-s2 cut and restored during downloads (least-loaded)",
                 "4_timeline_linkfail.png", out)
    run_timeline(batch, "serverfail", "static", 1,
                 "srv1 killed at 6 s: static policy", "5a_timeline_serverfail_static.png", out)
    run_timeline(batch, "serverfail", "least_loaded", 1,
                 "srv1 killed at 6 s: least-loaded policy",
                 "5b_timeline_serverfail_least_loaded.png", out)
    run_timeline(batch, "congestion", "path_delay", 1,
                 "Flood on the fast path: congestion-blind routing",
                 "6a_timeline_congestion_blind.png", out)
    run_timeline(batch, "congestion", "path_congestion", 1,
                 "Flood on the fast path: congestion-aware routing",
                 "6b_timeline_congestion_aware.png", out)


if __name__ == "__main__":
    main()
