#!/usr/bin/env python3
"""Rebuild the matched comparison from public compact metadata; no scientific runs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


def draw_outcome(ax: Any, center: float, y: float, outcome: dict[str, Any]) -> None:
    reason, count = outcome["terminal_reason"], outcome["generations_completed"]
    suffixes = {
        "model_exception": " E",
        "nondivision_max_duration": " N",
        "reached_max_generations": "",
    }
    if reason not in suffixes or not 0 <= count <= 20:
        raise ValueError("Unexpected terminal outcome")
    if reason == "reached_max_generations" and count != 20:
        raise ValueError("Completed lineage has fewer than 20 divisions")
    error = reason == "model_exception"
    ax.add_patch(
        Rectangle(
            (center - 0.49, y - 0.32),
            0.98,
            0.64,
            facecolor="#fff0dc" if error else plt.cm.Blues(0.08 + 0.76 * count / 20),
            edgecolor="#c7a06a" if error else "white",
            linewidth=0.8,
        )
    )
    ax.text(
        center,
        y,
        f"{count}{suffixes[reason]}",
        ha="center",
        va="center",
        color="white" if not error and count >= 14 else "#172033",
        weight="bold" if count == 20 else "normal",
    )


def plot_comparison(source: Path, output: Path) -> None:
    data = json.loads(source.read_bytes())
    rows = data["comparison_rows"]
    method_summary = data["method_summary"]
    genotype_counts = Counter(row["state_id"] for row in rows)
    seeds = [101, 211, 307]
    if len(rows) != 25 or any(
        [o["simulation_seed"] for o in row["outcomes"]] != seeds for row in rows
    ):
        raise ValueError(
            "Expected 25 frozen nominees and three ordered simulation seeds"
        )
    controls = data["controls"]
    if len(controls) != 6 or any(
        c["terminal_reason"] != "reached_max_generations"
        or c["generations_completed"] != 20
        for c in controls
    ):
        raise ValueError("Control outcomes do not match the frozen completed study")
    current_lines = {
        seed: {c["state_id"]: c for c in controls if c["simulation_seed"] == seed}
        for seed in seeds
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "pdf.fonttype": 42,
            "svg.fonttype": "none",
        }
    )
    fig, ax = plt.subplots(figsize=(11.5, 14.7))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 39)
    ax.axis("off")
    fig.text(
        0.06,
        0.972,
        "Deletion depth and whole-cell outcomes",
        fontsize=20,
        weight="bold",
    )
    fig.text(
        0.06,
        0.947,
        "Three simulation seeds per genome; original agent results plus the exploratory matched baselines",
        fontsize=10,
        color="#475569",
    )
    ax.text(0.1, 37.5, "Search seed", weight="bold")
    ax.text(2.0, 37.5, "Total genes deleted / 4,290", weight="bold")
    ax.text(7.65, 38, "Completed divisions", ha="center", weight="bold")
    for center, seed in zip([6.3, 7.8, 9.3], seeds):
        ax.text(center, 37.45, str(seed), ha="center", weight="bold")
    headings = [
        ("No retrieval", ["no_retrieval"]),
        ("Frozen dossier", ["frozen_dossier"]),
        ("Adaptive literature", ["adaptive_literature"]),
        ("Baselines", ["greedy_block", "randomized_block"]),
    ]
    y = 35.9
    scale = 3.5 / 2400
    barx = 2.0
    for heading, group in headings:
        ax.text(0.1, y, heading, fontsize=13, weight="bold", va="center")
        summaries = [
            "%s %d/5"
            % (
                {"greedy_block": "Greedy", "randomized_block": "Randomized"}.get(m, ""),
                method_summary[m]["all_three_reached20"],
            )
            for m in group
        ]
        ax.text(
            10.7,
            y,
            "; ".join(summaries) + " all 3 at 20",
            ha="right",
            va="center",
            fontsize=9,
            color="#475569",
        )
        y -= 1.0
        for method in group:
            for row in sorted(
                [r for r in rows if r["method"] == method],
                key=lambda r: r["search_seed"],
            ):
                label = (
                    (
                        "%s " % ("G" if method == "greedy_block" else "R")
                        if method in ["greedy_block", "randomized_block"]
                        else ""
                    )
                    + str(row["search_seed"])
                    + ("*" if genotype_counts[row["state_id"]] > 1 else "")
                )
                ax.text(0.15, y, label, va="center", fontsize=10)
                ax.add_patch(
                    Rectangle(
                        (barx, y - 0.2),
                        1030 * scale,
                        0.4,
                        facecolor="#cbd5e1",
                        edgecolor="none",
                    )
                )
                ax.add_patch(
                    Rectangle(
                        (barx + 1030 * scale, y - 0.2),
                        (row["deletions"] - 1030) * scale,
                        0.4,
                        facecolor="#475569"
                        if method != "randomized_block"
                        else "#8b5e3c",
                        edgecolor="none",
                    )
                )
                ax.text(
                    5.65,
                    y,
                    f"{row['deletions']:,}",
                    ha="right",
                    va="center",
                    fontsize=10,
                )
                for center, o in zip([6.3, 7.8, 9.3], row["outcomes"]):
                    draw_outcome(ax, center, y, o)
                y -= 0.82
            if len(group) > 1:
                y -= 0.25
        y -= 0.75
    ax.text(0.1, y, "Controls · current matched batch", fontsize=12, weight="bold")
    y -= 0.9
    for name, label in [
        ("mg1655-control", "MG1655"),
        ("starting-genome-control", "Common root"),
    ]:
        found = [
            next(x for x in current_lines[seed].values() if x["name"] == name)
            for seed in seeds
        ]
        ax.text(0.15, y, label, va="center")
        ax.text(5.65, y, f"{found[0]['deletion_count']:,}", ha="right", va="center")
        for center, line in zip([6.3, 7.8, 9.3], found):
            draw_outcome(ax, center, y, line)
        y -= 0.82
    fig.text(
        0.06,
        0.064,
        "N: no further division within 10,800 simulated seconds.  E: model/numerical error; viability unresolved.",
        fontsize=9,
    )
    fig.text(
        0.06,
        0.046,
        "G = greedy; R = randomized. * Shared genotype and trajectories. Grey bars: 1,030 inherited deletions.",
        fontsize=9,
    )
    fig.text(
        0.06,
        0.028,
        "Exploratory selection ceiling: 1,720 deletions; greedy 227 retained at 1,405. No outcome-based replacements.",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.045, right=0.98, top=0.925, bottom=0.09)
    for suffix in ["png", "pdf"]:
        fig.savefig(output.with_suffix("." + suffix), dpi=180, facecolor="white")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "experiments/comparisons/depth-matched-20261111.json",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Output stem for PNG and PDF"
    )
    args = parser.parse_args()
    plot_comparison(args.source, args.output)


if __name__ == "__main__":
    main()
