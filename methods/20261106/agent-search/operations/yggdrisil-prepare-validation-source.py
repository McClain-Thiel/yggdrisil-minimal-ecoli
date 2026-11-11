import hashlib,json,shutil
from pathlib import Path
ROOT=Path('/home/ubuntu/yggdrisil-continuation-20261106')
dest=ROOT/'validation-application'
if dest.exists():raise RuntimeError('Validation source already exists; inspect before modifying')
shutil.copytree(ROOT/'application/src',dest/'src',ignore=shutil.ignore_patterns('__pycache__'))
(dest/'scripts').mkdir();shutil.copy2(ROOT/'application/scripts/track_vecoli_experiment.py',dest/'scripts/track_vecoli_experiment.py')
p=dest/'src/yggdrisil_ecoli/vecoli.py';s=p.read_text();old='    if tracked_status:\n        raise ValueError("vEcoli checkout has tracked changes")'
new='''    if tracked_status:
        if tracked_status != "M wholecell/utils/_netflow/nf_glpk.py":
            raise ValueError("Unexpected tracked vEcoli changes")
        if file_sha256(checkout / "wholecell/utils/_netflow/nf_glpk.py") != "5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686":
            raise ValueError("Solver patch differs from the approved v2 recovery")'''
assert old in s;p.write_text(s.replace(old,new))
p=dest/'src/yggdrisil_ecoli/vecoli_results.py';s=p.read_text();needle='    for prefix in ("adapter", "variant_knockout_audit"):';assert needle in s
s=s.replace(needle,'''    if "solver_after_sha256" in vecoli:
        _verify_file(Path(vecoli["checkout"]) / "wholecell/utils/_netflow/nf_glpk.py",
                     vecoli["solver_after_sha256"], "strict solver recovery")
''' + needle);p.write_text(s)
p=dest/'scripts/track_vecoli_experiment.py';s=p.read_text();needle='    return files\n\n\ndef simulation_environment';assert needle in s
s=s.replace(needle,'''    files.extend(p for p in (task.workdir / "solver-diagnostics").glob("*")
                 if p.suffix in {".json", ".glp"})
    return files


def simulation_environment''')
s=s.replace('            "NXF_VER": VECOLI_NEXTFLOW_VERSION,','            "NXF_VER": VECOLI_NEXTFLOW_VERSION,\n            "YGGDRISIL_SOLVER_DIAGNOSTICS": "solver-diagnostics",')
# Initial backups include exact source graph snapshots before any simulation.
needle='            audit_path = manifest["vecoli"].get("variant_knockout_audit_path")'
assert needle in s;s=s.replace(needle,'''            for index, graph in enumerate(manifest.get("source_graphs", [])):
                initial_inputs[f"inputs/graph-{index}.sqlite"] = Path(graph["backup"]["frozen_path"])
''' + needle)
needle='def simulation_environment('
helper='''def solver_diagnostics(workdir):
    records = []
    for path in sorted((workdir / "solver-diagnostics").glob("*.json")):
        check = json.loads(path.read_text())
        before, after = path.with_suffix(".before.glp"), path.with_suffix(".after.glp")
        if (not check["model_unchanged"] or check["return_code"] != 0
                or not check["status"].startswith("GLP_OPT")
                or len(check["kkt"]) != 4
                or any(not math.isfinite(v["relative_error"]) or v["relative_error"] > 1e-7
                       for v in check["kkt"].values())
                or file_sha256(before) != check["original_lp_sha256"]
                or file_sha256(after) != check["original_lp_sha256"]):
            raise RuntimeError("Rejected solver diagnostic in completed cell: " + str(path))
        records.append({"path": str(path), "sha256": file_sha256(path),
                        "max_kkt_relative_error": max(v["relative_error"] for v in check["kkt"].values())})
    return records


'''
assert needle in s;s=s.replace(needle,helper+needle)
needle='                if key in checked:\n                    continue'
assert needle in s;s=s.replace(needle,'''                generation["solver_fallbacks"] = solver_diagnostics(tasks[key].workdir)
                generation["solver_fallback_assisted"] = bool(generation["solver_fallbacks"])
''' + needle)
needle='        if tasks:\n            for path in (experiment / "variant_sim_data").glob("*"):'
assert needle in s;s=s.replace(needle,'''        for marker in (experiment / "nextflow/nextflow_workdirs").rglob(".exitcode"):
            for diagnostic in (marker.parent / "solver-diagnostics").glob("*"):
                if diagnostic.suffix in {".json", ".glp"}:
                    add_data(diagnostic, "output/" + str(diagnostic.relative_to(experiment)))
''' + needle)
p.write_text(s)
for p in dest.rglob('*.py'):compile(p.read_text(),str(p),'exec')
print('Prepared isolated validation code with exact patch allowlist and per-cell diagnostic preservation')
