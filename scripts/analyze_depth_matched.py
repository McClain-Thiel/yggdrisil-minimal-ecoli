import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _():
    import hashlib
    import json
    import math
    from collections import Counter
    from pathlib import Path

    import marimo as mo
    import matplotlib
    import matplotlib.pyplot as plt
    import polars as pl
    from matplotlib.lines import Line2D

    return Counter, Line2D, Path, hashlib, json, math, matplotlib, mo, pl, plt


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Deletion depth and whole-cell validation

    **Exploratory comparison · 11 November 2026**

    This notebook reads the verified compact results only. It runs no searches,
    simulations, model calls or cloud jobs. Original attempts and results stay unchanged.

    The unit for method-level success is a **search nominee**, with three simulation
    seeds per genome. Numerical errors are unresolved outcomes, not biological failures.
    The two baseline nominees with the same genotype share their three trajectories;
    they are counted once in pooled physical-lineage and cycle summaries.
    """)
    return


@app.cell(hide_code=True)
def _(Counter, Path, hashlib, json, math, mo, pl):
    repo = Path(__file__).resolve().parents[1]
    source = repo / "experiments/comparisons/depth-matched-20261111.json"
    source_sha256 = "79b77329c643f5d799fe67fc85ab40d3c2a6212fd1bd18c49e382f78ca1adf3e"
    _raw = source.read_bytes()
    if hashlib.sha256(_raw).hexdigest() != source_sha256:
        raise ValueError(
            "The frozen comparison changed; inspect provenance before updating the analysis."
        )
    comparison = json.loads(_raw)
    method_order = [
        "no_retrieval",
        "frozen_dossier",
        "adaptive_literature",
        "greedy_block",
        "randomized_block",
    ]
    method_labels = {
        "no_retrieval": "No retrieval",
        "frozen_dossier": "Frozen dossier",
        "adaptive_literature": "Adaptive literature",
        "greedy_block": "Greedy",
        "randomized_block": "Randomized",
    }
    method_colors = dict(
        zip(method_order, ["#0072B2", "#CC79A7", "#009E73", "#E69F00", "#D55E00"])
    )
    endpoint_labels = {
        "reached_max_generations": "20 divisions",
        "nondivision_max_duration": "Nondivision at cap",
        "model_exception": "Model / numerical error",
    }
    endpoint_markers = {
        "reached_max_generations": "o",
        "nondivision_max_duration": "v",
        "model_exception": "X",
    }
    headings = [
        ("No retrieval", ["no_retrieval"]),
        ("Frozen dossier", ["frozen_dossier"]),
        ("Adaptive literature", ["adaptive_literature"]),
        ("Baselines", ["greedy_block", "randomized_block"]),
    ]
    _rows, _lines, _cycles, _seen = [], [], [], {}
    for _row in comparison["comparison_rows"]:
        _outcomes = _row["outcomes"]
        if sorted(o["simulation_seed"] for o in _outcomes) != [101, 211, 307]:
            raise ValueError("Missing or duplicate simulation seed")
        _counts = Counter(o["terminal_reason"] for o in _outcomes)
        _all_three = _counts["reached_max_generations"] == 3
        if _all_three != _row["all_three_reached20"]:
            raise ValueError("Recorded nominee success disagrees with its trajectories")
        _shared = (
            sum(
                r["state_id"] == _row["state_id"] for r in comparison["comparison_rows"]
            )
            > 1
        )
        _base = {
            k: _row[k]
            for k in (
                "method",
                "search_seed",
                "state_id",
                "study",
                "source_run_id",
                "deletions",
            )
        }
        _rows.append(
            {
                **_base,
                "registry_percent": 100 * _row["deletions"] / 4290,
                "all_three_at20": _all_three,
                "at20": _counts["reached_max_generations"],
                "nondivision": _counts["nondivision_max_duration"],
                "errors": _counts["model_exception"],
                "shared_genotype": _shared,
            }
        )
        for _outcome in _outcomes:
            _n = _outcome["generations_completed"]
            _reason = _outcome["terminal_reason"]
            if (
                _reason not in endpoint_labels
                or type(_n) is not int
                or not 0 <= _n <= 20
            ):
                raise ValueError("Unexpected endpoint or division count")
            if (_reason == "reached_max_generations") != (_n == 20):
                raise ValueError("Endpoint label disagrees with the generation count")
            _durations, _folds = (
                _outcome["generation_durations_s"],
                _outcome["dry_mass_fold_changes"],
            )
            if (
                len(_durations) != _n
                or len(_folds) != _n
                or any(not math.isfinite(v) or v <= 0 for v in _durations + _folds)
            ):
                raise ValueError(
                    "Missing, nonfinite or nonpositive closed-cycle measurements"
                )
            _key = (_row["study"], _row["state_id"], _outcome["simulation_seed"])
            if _key in _seen and _seen[_key] != _outcome:
                raise ValueError("Shared genotype has conflicting trajectory records")
            _seen[_key] = _outcome
            _line = {
                **_base,
                "simulation_seed": _outcome["simulation_seed"],
                "divisions": _n,
                "endpoint": _reason,
                "shared_genotype": _shared,
            }
            _lines.append(_line)
            for _generation, (_duration, _fold) in enumerate(
                zip(_durations, _folds), 1
            ):
                _cycles.append(
                    {
                        **_line,
                        "generation": _generation,
                        "division_time_min": _duration / 60,
                        "dry_mass_fold": _fold,
                    }
                )
    nominees = pl.DataFrame(_rows)
    lineages = pl.DataFrame(_lines)
    cycles = pl.DataFrame(_cycles)
    physical_lineages = lineages.unique(
        subset=["study", "state_id", "simulation_seed"], maintain_order=True
    )
    physical_cycles = cycles.unique(
        subset=["study", "state_id", "simulation_seed", "generation"],
        maintain_order=True,
    )
    controls = pl.DataFrame(comparison["controls"])
    if nominees.height != 25 or physical_lineages.height != 72 or controls.height != 6:
        raise ValueError("Unexpected cohort size")
    for _method in method_order:
        _m = nominees.filter(pl.col("method") == _method)
        _terminal = Counter(
            lineages.filter(pl.col("method") == _method)["endpoint"].to_list()
        )
        _expected = comparison["method_summary"][_method]
        if (
            _m.height != _expected["assigned_searches"]
            or _m["all_three_at20"].sum() != _expected["all_three_reached20"]
            or dict(_terminal) != _expected["terminal_counts"]
        ):
            raise ValueError(f"Summary disagrees with original outcomes: {_method}")
    mo.md(
        f"Verified **{nominees.height} nominees, {physical_lineages.height} unique candidate lineages, {physical_cycles.height} closed candidate cycles**, plus six control lineages. Source SHA256: `{source_sha256}`."
    )
    return (
        controls,
        endpoint_labels,
        endpoint_markers,
        headings,
        lineages,
        method_colors,
        method_labels,
        method_order,
        nominees,
        physical_cycles,
        physical_lineages,
        repo,
        source,
        source_sha256,
    )


@app.cell(hide_code=True)
def _(method_labels, method_order, mo, nominees, pl):
    _summary = []
    for _method in method_order:
        _m = nominees.filter(pl.col("method") == _method)
        _summary.append(
            {
                "Method": method_labels[_method],
                "Search nominees": _m.height,
                "All 3 seeds at 20": int(_m["all_three_at20"].sum()),
                "Lineages at 20": int(_m["at20"].sum()),
                "Nondivision": int(_m["nondivision"].sum()),
                "Numerical/model errors": int(_m["errors"].sum()),
                "Min deletions": _m["deletions"].min(),
                "Median deletions": _m["deletions"].median(),
                "Max deletions": _m["deletions"].max(),
            }
        )
    method_table = pl.DataFrame(_summary)
    mo.vstack(
        [
            mo.md(
                "## Descriptive summary\nFive assigned search nominees per method. The greedy and randomized rows each include their shared genotype; do not sum them as independent physical lineages."
            ),
            mo.ui.table(method_table, selection=None),
        ]
    )
    return (method_table,)


@app.cell(hide_code=True)
def _(
    Line2D,
    endpoint_labels,
    endpoint_markers,
    headings,
    lineages,
    method_colors,
    method_labels,
    method_order,
    pl,
    plt,
):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "pdf.fonttype": 42,
            "svg.fonttype": "none",
        }
    )
    endpoint_figure, _axes = plt.subplots(
        1, 4, figsize=(14.6, 4.6), sharex=True, sharey=True
    )
    for _ax, (_heading, _methods) in zip(_axes, headings):
        for _method in _methods:
            _subset = lineages.filter(pl.col("method") == _method)
            for _r in _subset.iter_rows(named=True):
                _offset = {101: -7, 211: 0, 307: 7}[_r["simulation_seed"]]
                _ax.scatter(
                    _r["deletions"] + _offset,
                    _r["divisions"],
                    marker=endpoint_markers[_r["endpoint"]],
                    color=method_colors[_method],
                    s=47,
                    alpha=0.8,
                    edgecolors="white",
                    linewidths=0.35,
                )
        _ax.set_title(_heading, weight="bold")
        _ax.set_xlabel("Total genes deleted")
        _ax.set_xlim(1000, 2400)
        _ax.set_xticks([1000, 1500, 2000])
        _ax.set_ylim(-0.8, 21.3)
        _ax.set_yticks([0, 5, 10, 15, 20])
        _ax.grid(axis="y", alpha=0.2)
        _ax.spines[["right", "top"]].set_visible(False)
    _axes[0].set_ylabel("Completed divisions")
    _handles = [
        Line2D(
            [],
            [],
            color="#475569",
            marker=_marker,
            linestyle="none",
            label=endpoint_labels[_reason],
        )
        for _reason, _marker in endpoint_markers.items()
    ]
    _handles += [
        Line2D(
            [],
            [],
            color=method_colors[_m],
            marker="o",
            linestyle="none",
            label=method_labels[_m],
        )
        for _m in method_order[-2:]
    ]
    endpoint_figure.legend(
        handles=_handles,
        loc="lower center",
        ncol=5,
        frameon=False,
        bbox_to_anchor=(0.5, 0.075),
    )
    endpoint_figure.suptitle(
        "Whole-cell outcomes across deletion depths", fontsize=16, weight="bold"
    )
    endpoint_figure.text(
        0.5,
        0.015,
        "Each point is a simulation seed; horizontal offsets of ±7 genes reveal overlaps. Shared baseline trajectories appear in both colors.\nNondivision = no next division by 10,800 s; errors leave viability unresolved. Registry denominator: 4,290; 1,030 deletions inherited.",
        ha="center",
        fontsize=8,
    )
    endpoint_figure.tight_layout(rect=(0, 0.19, 1, 0.94))
    endpoint_figure
    return (endpoint_figure,)


@app.cell(hide_code=True)
def _(headings, method_colors, method_labels, nominees, pl, plt):
    success_figure, _axes = plt.subplots(
        1,
        4,
        figsize=(12, 3.7),
        sharey=True,
        gridspec_kw={"width_ratios": [1, 1, 1, 1.7]},
    )
    for _ax, (_heading, _methods) in zip(_axes, headings):
        for _x, _method in enumerate(_methods):
            _m = nominees.filter(pl.col("method") == _method)
            _success = int(_m["all_three_at20"].sum())
            _ax.bar(_x, _success, color=method_colors[_method], width=0.55)
            _ax.text(_x, _success + 0.13, f"{_success}/5", ha="center", weight="bold")
        _ax.set_title(_heading, weight="bold")
        _ax.set_xticks(
            range(len(_methods)),
            [method_labels[m] if len(_methods) > 1 else "" for m in _methods],
        )
        _ax.set_xlim(-0.7, len(_methods) - 0.3)
        _ax.set_ylim(0, 5.3)
        _ax.set_yticks(range(6))
        _ax.grid(axis="y", alpha=0.2)
        _ax.spines[["top", "right"]].set_visible(False)
    _axes[0].set_ylabel("Nominees with all 3 seeds at 20")
    success_figure.suptitle(
        "Repeatable endpoint completion within each nominee", fontsize=15, weight="bold"
    )
    success_figure.text(
        0.5,
        0.015,
        "Descriptive counts, five search nominees per method. Different realized search budgets and deletion depths; no significance claim.\nFailure to meet this endpoint includes unresolved model errors. Greedy/randomized share one genome.",
        ha="center",
        fontsize=8,
    )
    success_figure.tight_layout(rect=(0, 0.13, 1, 0.94))
    success_figure
    return (success_figure,)


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Growth dynamics and diagnostics
    Only **closed, dividing cells** contribute points below. These are conditional
    trajectories, not a survival estimate. Stopped or erroneous lineages do not acquire
    extra zeros. The mass plot uses a log scale and retains all large values.
    """)
    return


@app.cell(hide_code=True)
def _(
    Line2D,
    headings,
    method_colors,
    method_labels,
    method_order,
    physical_cycles,
    pl,
    plt,
):
    growth_figure, _axes = plt.subplots(
        2, 4, figsize=(14.6, 6.8), sharex=True, sharey="row"
    )
    for _column, (_heading, _methods) in enumerate(headings):
        _subset = physical_cycles.filter(pl.col("method").is_in(_methods))
        for _trajectory in _subset.partition_by(
            ["study", "state_id", "simulation_seed"], maintain_order=True
        ):
            _trajectory = _trajectory.sort("generation")
            _color = method_colors[_trajectory["method"][0]]
            _axes[0, _column].plot(
                _trajectory["generation"],
                _trajectory["division_time_min"],
                color=_color,
                alpha=0.6,
                linewidth=0.9,
                marker=".",
                markersize=3,
            )
            _axes[1, _column].plot(
                _trajectory["generation"],
                _trajectory["dry_mass_fold"],
                color=_color,
                alpha=0.6,
                linewidth=0.9,
                marker=".",
                markersize=3,
            )
        _axes[0, _column].set_title(_heading, weight="bold")
        _axes[1, _column].set_xlabel("Generation")
        _axes[1, _column].set_yscale("log")
        _axes[1, _column].axhline(2, color="#64748b", linestyle=":", linewidth=0.8)
        for _ax in _axes[:, _column]:
            _ax.set_xlim(0.5, 20.5)
            _ax.set_xticks([1, 5, 10, 15, 20])
            _ax.grid(axis="y", alpha=0.18)
            _ax.spines[["right", "top"]].set_visible(False)
    _axes[0, 0].set_ylabel("Division time (minutes)")
    _axes[1, 0].set_ylabel("Dry mass at division / birth\n(log scale)")
    growth_figure.suptitle(
        "Growth trajectories, with large mass ratios retained",
        fontsize=15,
        weight="bold",
    )
    growth_figure.legend(
        handles=[
            Line2D([], [], color=method_colors[_m], label=method_labels[_m])
            for _m in method_order[-2:]
        ],
        loc="lower right",
        bbox_to_anchor=(0.98, 0.08),
        ncol=2,
        frameon=False,
    )
    growth_figure.text(
        0.5,
        0.025,
        "One line per physical candidate trajectory; the shared baseline genome is drawn once. Only completed cycles are shown.\nDotted reference: twofold dry-mass growth. Controls lack cycle-level metrics in this compact file and are not imputed.",
        ha="center",
        fontsize=8,
    )
    growth_figure.tight_layout(rect=(0, 0.08, 1, 0.95))
    growth_figure
    return (growth_figure,)


@app.cell(hide_code=True)
def _(method_labels, mo):
    method_filter = mo.ui.dropdown(
        {
            "All methods": "all",
            **{label: method for method, label in method_labels.items()},
        },
        value="All methods",
        label="Inspect nominees",
    )
    method_filter
    return (method_filter,)


@app.cell(hide_code=True)
def _(method_filter, mo, nominees, pl):
    _selected = (
        nominees
        if method_filter.value == "all"
        else nominees.filter(pl.col("method") == method_filter.value)
    )
    mo.ui.table(
        _selected.select(
            "method",
            "search_seed",
            "deletions",
            "registry_percent",
            "at20",
            "nondivision",
            "errors",
            "shared_genotype",
            "state_id",
            "source_run_id",
        ),
        selection=None,
    )
    return


@app.cell(hide_code=True)
def _(controls, mo, physical_cycles, pl):
    inspection_flags = physical_cycles.filter(
        (pl.col("dry_mass_fold") > 4) | (pl.col("dry_mass_fold") < 1)
    )
    mo.vstack(
        [
            mo.md(
                f"### Values to inspect: {inspection_flags.height} closed cycles\nThis descriptive flag shows dry-mass ratios outside 1–4; it does **not** change any original endpoint classification or exclude any result."
            ),
            mo.ui.table(
                inspection_flags.select(
                    "method",
                    "search_seed",
                    "simulation_seed",
                    "generation",
                    "division_time_min",
                    "dry_mass_fold",
                    "endpoint",
                ),
                selection=None,
            ),
            mo.md("### Control endpoint check"),
            mo.ui.table(controls, selection=None),
        ]
    )
    return (inspection_flags,)


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## What these results support

    Adaptive literature has **4/5 nominees** meeting the three-seed endpoint, compared
    with **2/5** for each other agent method and **0/5** for each deeper baseline.
    That is a promising descriptive pattern. The depth plot also shows why a pooled
    percentage alone would be misleading: the panels do not occupy identical deletion
    ranges, and numerical errors affect several agent nominees.

    The new baseline batch contains **23 physical lineages with nondivision and four
    with model exceptions**, with six control lineages reaching 20. Those errors are
    not evidence of biological death. Twenty divisions in this model are not proof
    of laboratory viability.

    Next analyses should reconcile gene denominators, inspect the large mass ratios
    and error traces, and compare deletion sets/functions with an explicit background
    gene universe. Any fresh diagnostic replay should remain a separate, authorized
    experiment. This notebook makes no new execution requests.
    """)
    return


@app.cell(hide_code=True)
def _(
    controls,
    endpoint_figure,
    growth_figure,
    hashlib,
    inspection_flags,
    json,
    matplotlib,
    method_table,
    mo,
    nominees,
    physical_cycles,
    physical_lineages,
    pl,
    repo,
    source,
    source_sha256,
    success_figure,
):
    figure_folder = repo / "experiments/figures/depth-matched-analysis-20261111"
    figure_folder.mkdir(parents=True, exist_ok=True)
    figure_files = []
    for _name, _figure in [
        ("deletion-depth-outcomes", endpoint_figure),
        ("nominee-endpoint-success", success_figure),
        ("growth-dynamics", growth_figure),
    ]:
        for _suffix in ("png", "pdf"):
            _path = figure_folder / f"{_name}.{_suffix}"
            _metadata = (
                {"CreationDate": None, "ModDate": None} if _suffix == "pdf" else {}
            )
            _figure.savefig(
                _path,
                dpi=110 if _name == "growth-dynamics" else 130,
                facecolor="white",
                metadata=_metadata,
            )
            _content = _path.read_bytes()
            figure_files.append(
                {
                    "path": str(_path.relative_to(repo)),
                    "sha256": hashlib.sha256(_content).hexdigest(),
                    "bytes": len(_content),
                }
            )
    analysis_summary = {
        "schema_version": 1,
        "kind": "descriptive_analysis_no_new_science",
        "source": str(source.relative_to(repo)),
        "source_sha256": source_sha256,
        "notebook": "scripts/analyze_depth_matched.py",
        "registry_denominator": 4290,
        "inherited_deletions": 1030,
        "nominees": nominees.height,
        "physical_candidate_lineages": physical_lineages.height,
        "closed_candidate_cycles": physical_cycles.height,
        "control_lineages": controls.height,
        "method_summary": method_table.to_dicts(),
        "mass_ratio_inspection_flags": inspection_flags.height,
        "versions": {
            "marimo": mo.__version__,
            "matplotlib": matplotlib.__version__,
            "polars": pl.__version__,
        },
        "figures": figure_files,
        "interpretation": "Exploratory, unequal realized budgets and deletion depths; model exceptions unresolved; shared baseline genotype counted once in pooled physical summaries; not laboratory viability.",
    }
    analysis_path = (
        repo / "experiments/comparisons/depth-matched-analysis-20261111.json"
    )
    analysis_path.write_text(
        json.dumps(analysis_summary, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    mo.md(
        f"Saved **{len(figure_files)} figure files** (PNG and PDF) and a compact provenance summary. Figures: `{figure_folder.relative_to(repo)}`."
    )
    return


if __name__ == "__main__":
    app.run()
