import marimo

__generated_with = "0.24.2"
app = marimo.App(width="medium")


@app.cell
def imports():
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    import marimo as mo

    from yggdrisil_ecoli.data import sources
    from yggdrisil_ecoli.data.audit import audit_registry
    from yggdrisil_ecoli.data.crosswalks import add_crosswalks
    from yggdrisil_ecoli.data.essentiality import parse_choe_workbook
    from yggdrisil_ecoli.data.evidence import validate_genes, write_genes
    from yggdrisil_ecoli.data.gff import parse_ncbi_gff
    from yggdrisil_ecoli.data.io import atomic_json, file_sha256
    from yggdrisil_ecoli.data.reduced_genomes import build_validation
    from yggdrisil_ecoli.module_build import build_kegg_modules

    return (
        Path,
        UTC,
        add_crosswalks,
        atomic_json,
        audit_registry,
        build_kegg_modules,
        build_validation,
        datetime,
        file_sha256,
        json,
        mo,
        parse_choe_workbook,
        parse_ncbi_gff,
        sources,
        validate_genes,
        write_genes,
    )


@app.cell
def introduction(mo):
    mo.md("""
    # Prepare the MG1655 evidence

    Run this recipe once, then use the resulting `processed/genes.parquet`,
    `processed/kegg_modules.json` and `external/iML1515.json` in the experiment.
    Source files stay in the local cache. The manifest records their hashes.
    KEGG material needs redistribution permission before publication to Hugging Face.
    """)
    return


@app.cell
def settings(mo):
    data_folder = mo.ui.text(value="data", label="Dataset folder")
    accept_kegg = mo.ui.checkbox(label="I am permitted to use the KEGG academic API")
    prepare = mo.ui.run_button(label="Prepare evidence")
    mo.vstack([data_folder, accept_kegg, prepare])
    return accept_kegg, data_folder, prepare


@app.cell
def acquire(Path, accept_kegg, data_folder, mo, prepare, sources):
    mo.stop(not prepare.value, mo.md("Choose a folder and prepare the evidence."))
    mo.stop(not accept_kegg.value, mo.md("Confirm KEGG access before downloading."))
    data_dir = Path(data_folder.value).expanduser()
    raw, processed = data_dir / "raw", data_dir / "processed"
    specs = (
        sources.NCBI_GFF,
        sources.KEGG_GENE_LIST,
        sources.KEGG_KO_LINKS,
        sources.IML1515_PUBLICATION_ARCHIVE,
        sources.CHOE_2023_SUPPLEMENT_BUNDLE,
    )
    # Reuse cached source bytes. Set refresh=True only to deliberately update sources.
    inputs = {spec.filename: sources.acquire_source(spec, raw) for spec in specs}
    model_path = sources.extract_member(
        inputs[sources.IML1515_PUBLICATION_ARCHIVE.filename],
        sources.IML1515_PUBLICATION_MEMBER,
        sources.IML1515_PUBLICATION_MEMBER_SHA256,
        data_dir / "external" / "iML1515.json",
    )
    return data_dir, inputs, model_path, processed, raw, specs


@app.cell
def reference_genes(
    add_crosswalks,
    audit_registry,
    inputs,
    model_path,
    parse_ncbi_gff,
    sources,
):
    reference_genes, reference_metadata = parse_ncbi_gff(
        inputs[sources.NCBI_GFF.filename]
    )
    registry, crosswalks = add_crosswalks(
        reference_genes,
        inputs[sources.KEGG_GENE_LIST.filename],
        inputs[sources.KEGG_KO_LINKS.filename],
        model_path,
    )
    crosswalk_audit = {**audit_registry(registry), **crosswalks}
    registry.head()
    return crosswalk_audit, reference_metadata, registry


@app.cell
def join_evidence(
    file_sha256,
    inputs,
    parse_choe_workbook,
    raw,
    reference_metadata,
    registry,
    sources,
    validate_genes,
):
    _workbook = sources.extract_member(
        inputs[sources.CHOE_2023_SUPPLEMENT_BUNDLE.filename],
        sources.CHOE_2023_MEMBER,
        sources.CHOE_2023_MEMBER_SHA256,
        raw / sources.CHOE_2023_MEMBER,
    )
    measurements, essentiality_audit = parse_choe_workbook(
        _workbook,
        registry,
        metadata={
            "provenance": {
                "workbook_sha256": sources.CHOE_2023_MEMBER_SHA256,
                "reference_gff_sha256": file_sha256(inputs[sources.NCBI_GFF.filename]),
            }
        },
    )
    genes = registry.join(measurements, validate="one_to_one")
    genes.attrs = {
        "reference": reference_metadata,
        "essentiality": measurements.attrs["essentiality"],
    }
    genes = validate_genes(genes)
    genes.groupby(["coverage", "classification"]).size().rename("genes")
    return essentiality_audit, genes


@app.cell
def write_dataset(
    UTC,
    atomic_json,
    build_kegg_modules,
    crosswalk_audit,
    data_dir,
    datetime,
    essentiality_audit,
    file_sha256,
    genes,
    inputs,
    mo,
    model_path,
    processed,
    reference_metadata,
    sources,
    specs,
    write_genes,
):
    genes_path = processed / "genes.parquet"
    write_genes(genes, genes_path)
    manifest_path = processed / "source_manifest.json"
    atomic_json(
        manifest_path,
        {
            "built_at": datetime.now(UTC).isoformat(),
            "reference": reference_metadata,
            "inputs": {
                spec.filename: {
                    "url": spec.url,
                    "sha256": file_sha256(inputs[spec.filename]),
                }
                for spec in specs
            },
            "outputs": {
                path.name: file_sha256(path) for path in (genes_path, model_path)
            },
            "audit": {
                "crosswalks": crosswalk_audit,
                "essentiality": essentiality_audit,
            },
        },
    )
    build_kegg_modules(
        genes_path=genes_path,
        ko_links_path=inputs[sources.KEGG_KO_LINKS.filename],
        data_dir=data_dir,
        accept_kegg_terms=True,
        refresh=False,
    )
    mo.md(
        f"Prepared **{len(genes):,} genes** in `{data_dir}`. Review the manifest before publishing a dataset."
    )
    return genes_path, manifest_path


@app.cell
def heldout_settings(data_dir, mo):
    heldout_folder = mo.ui.text(
        value=str(data_dir / "validation"), label="Held-out source folder"
    )
    prepare_labels = mo.ui.run_button(label="Prepare held-out labels")
    mo.vstack(
        [
            mo.md("""
    ## Optional held-out labels

    These labels are for post-hoc comparison and stay outside the search inputs.
    Put `NC_000913.3.ncbi.json`, `AP012306.ncbi.json`, and
    `MS56_Park_2014_supplement.pdf` in the folder below. The sequence files are the
    NCBI fetch output; deriving MDS42 deletions also requires `minimap2` on PATH.
    """),
            heldout_folder,
            prepare_labels,
        ]
    )
    return heldout_folder, prepare_labels


@app.cell
def heldout_labels(
    Path,
    atomic_json,
    build_validation,
    data_dir,
    file_sha256,
    genes_path,
    heldout_folder,
    json,
    manifest_path,
    mo,
    prepare_labels,
):
    mo.stop(not prepare_labels.value, mo.md("Held-out preparation is optional."))
    _validation = Path(heldout_folder.value).expanduser()
    labels = build_validation(
        genes_path=genes_path,
        reference_path=_validation / "NC_000913.3.ncbi.json",
        mds42_path=_validation / "AP012306.ncbi.json",
        ms56_pdf_path=_validation / "MS56_Park_2014_supplement.pdf",
    )
    label_path = data_dir / "validation" / "reduced_genomes.json"
    atomic_json(label_path, labels)
    _manifest = json.loads(manifest_path.read_text())
    _manifest["outputs"][label_path.name] = file_sha256(label_path)
    _manifest["inputs"].update(
        {
            name: {"sha256": file_sha256(_validation / name)}
            for name in (
                "NC_000913.3.ncbi.json",
                "AP012306.ncbi.json",
                "MS56_Park_2014_supplement.pdf",
            )
        }
    )
    atomic_json(manifest_path, _manifest)
    {
        name: len(strain["deleted_gene_ids"])
        for name, strain in labels["strains"].items()
    }
    return


if __name__ == "__main__":
    app.run()
