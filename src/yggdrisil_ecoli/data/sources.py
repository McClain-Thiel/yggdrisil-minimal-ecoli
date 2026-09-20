"""Pinned source files for preparation, downloaded with Pooch.

NCBI: https://www.ncbi.nlm.nih.gov/home/about/policies/
Choe Table S1: CC-BY-4.0, doi:10.1128/msystems.00896-22.
iML1515: publication supplement, doi:10.1038/nbt.3956.
KEGG: academic API; permission is required to redistribute its snapshots.
"""

import hashlib
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pooch

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.io import atomic_bytes
from yggdrisil_ecoli.data.registry import file_sha256


@dataclass(frozen=True)
class SourceSpec:
    url: str
    filename: str
    expected_sha256: str | None = None


NCBI_GFF = SourceSpec(
    "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
    "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gff.gz",
    "GCF_000005845.2_ASM584v2_genomic.gff.gz",
    "7aa71ffaef2caa51e5cb00da96d567c8001c19f029a173d3df3b273331a587b2",
)
KEGG_GENE_LIST = SourceSpec("https://rest.kegg.jp/list/eco", "kegg_eco_genes.tsv")
KEGG_KO_LINKS = SourceSpec("https://rest.kegg.jp/link/ko/eco", "kegg_eco_ko_links.tsv")
KEGG_MODULE_LINKS = SourceSpec(
    "https://rest.kegg.jp/link/module/eco", "kegg_eco_complete_module_links.tsv"
)
KEGG_MODULE_INFO = SourceSpec(
    "https://rest.kegg.jp/info/module", "kegg_module_info.txt"
)
IML1515_PUBLICATION_ARCHIVE = SourceSpec(
    "https://media.springernature.com/original/springer-static/esm/"
    "art%3A10.1038%2Fnbt.3956/MediaObjects/41587_2017_BFnbt3956_MOESM30_ESM.zip",
    "Monk_2017_iML1515_models.zip",
    "e799bb0e266224f3f79a63ccffad98d4bec9a9aa29b4884de86be177138770a1",
)
IML1515_PUBLICATION_MEMBER = "Supplementary Data File 1 - Models/iML1515.json"
IML1515_PUBLICATION_MEMBER_SHA256 = (
    "832e706681b60eeefce844348dd7ded1520b7f8d2c2d72d423e0b77cc473dc45"
)
CHOE_2023_SUPPLEMENT_BUNDLE = SourceSpec(
    "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9948719/supplementaryFiles",
    "PMC9948719_supplementary_files.zip",
)
CHOE_2023_MEMBER = "msystems.00896-22-s0002.xlsx"
CHOE_2023_MEMBER_SHA256 = (
    "b1b27667bb9671e0cf031c46bb91e99077e759f4ccd5f75642c809e4d8b9595e"
)


def acquire_source(spec: SourceSpec, directory: Path, *, refresh: bool = False) -> Path:
    """Reuse frozen bytes or replace them only after a successful verified download."""

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / spec.filename
    if refresh or not destination.exists():
        if spec.url.startswith("https://rest.kegg.jp/"):
            time.sleep(0.35)  # KEGG requires fewer than three requests per second.
        with tempfile.TemporaryDirectory(dir=directory) as staging:
            downloaded = pooch.retrieve(
                spec.url,
                known_hash=spec.expected_sha256,
                fname=spec.filename,
                path=staging,
                downloader=pooch.HTTPDownloader(timeout=60),
            )
            Path(downloaded).replace(destination)
    if spec.expected_sha256 and file_sha256(destination) != spec.expected_sha256:
        raise DataValidationError(f"{destination}: source checksum changed")
    return destination


def extract_member(archive: Path, member: str, sha256: str, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        content = bundle.read(member)
    digest = hashlib.sha256(content).hexdigest()
    if digest != sha256:
        raise DataValidationError(f"{member}: expected {sha256}, got {digest}")
    atomic_bytes(destination, content)
    return destination
