"""Content-addressed acquisition of small, build-time reference sources."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from yggdrisil_ecoli.constants import ASSEMBLY_ACCESSION
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.registry import file_sha256

NCBI_GFF_URL = (
    "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
    "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gff.gz"
)
NCBI_GFF_SHA256 = "7aa71ffaef2caa51e5cb00da96d567c8001c19f029a173d3df3b273331a587b2"
KEGG_GENE_LIST_URL = "https://rest.kegg.jp/list/eco"
KEGG_KO_LINK_URL = "https://rest.kegg.jp/link/ko/eco"
KEGG_MODULE_LINK_URL = "https://rest.kegg.jp/link/module/eco"
KEGG_MODULE_INFO_URL = "https://rest.kegg.jp/info/module"
IML1515_PUBLICATION_ARCHIVE_URL = (
    "https://media.springernature.com/original/springer-static/esm/"
    "art%3A10.1038%2Fnbt.3956/MediaObjects/41587_2017_BFnbt3956_MOESM30_ESM.zip"
)
IML1515_PUBLICATION_ARCHIVE_SHA256 = (
    "e799bb0e266224f3f79a63ccffad98d4bec9a9aa29b4884de86be177138770a1"
)
IML1515_PUBLICATION_MEMBER = "Supplementary Data File 1 - Models/iML1515.json"
IML1515_PUBLICATION_MEMBER_SHA256 = (
    "832e706681b60eeefce844348dd7ded1520b7f8d2c2d72d423e0b77cc473dc45"
)
CHOE_2023_SUPPLEMENT_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9948719/supplementaryFiles"
)
CHOE_2023_MEMBER = "msystems.00896-22-s0002.xlsx"
CHOE_2023_MEMBER_SHA256 = (
    "b1b27667bb9671e0cf031c46bb91e99077e759f4ccd5f75642c809e4d8b9595e"
)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    url: str
    filename: str
    source_version: str
    redistribution: str
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    name: str
    url: str
    local_path: str
    source_version: str
    accessed_at: str
    sha256: str
    bytes: int
    cache_reused: bool
    redistribution: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


NCBI_GFF = SourceSpec(
    name="ncbi_refseq_gff3",
    url=NCBI_GFF_URL,
    filename="GCF_000005845.2_ASM584v2_genomic.gff.gz",
    source_version=ASSEMBLY_ACCESSION,
    redistribution="NCBI data; generated artifact is not vendored by this project",
    expected_sha256=NCBI_GFF_SHA256,
)
KEGG_GENE_LIST = SourceSpec(
    name="kegg_eco_gene_list",
    url=KEGG_GENE_LIST_URL,
    filename="kegg_eco_genes.tsv",
    source_version="live REST snapshot (content hash frozen in manifest)",
    redistribution="academic-use API snapshot; do not redistribute",
)
KEGG_KO_LINKS = SourceSpec(
    name="kegg_eco_ko_links",
    url=KEGG_KO_LINK_URL,
    filename="kegg_eco_ko_links.tsv",
    source_version="live REST snapshot (content hash frozen in manifest)",
    redistribution="academic-use API snapshot; do not redistribute",
)
KEGG_MODULE_LINKS = SourceSpec(
    name="kegg_eco_complete_module_links",
    url=KEGG_MODULE_LINK_URL,
    filename="kegg_eco_complete_module_links.tsv",
    source_version="live REST snapshot (content hash frozen in manifest)",
    redistribution="academic-use API snapshot; do not redistribute",
)
KEGG_MODULE_INFO = SourceSpec(
    name="kegg_module_database_info",
    url=KEGG_MODULE_INFO_URL,
    filename="kegg_module_info.txt",
    source_version="live REST snapshot (release/date recorded in content)",
    redistribution="academic-use API snapshot; do not redistribute",
)
IML1515_PUBLICATION_ARCHIVE = SourceSpec(
    name="monk_2017_iml1515_supplement",
    url=IML1515_PUBLICATION_ARCHIVE_URL,
    filename="Monk_2017_iML1515_models.zip",
    source_version="Monk et al. 2017 Supplementary Data Set 1; DOI 10.1038/nbt.3956",
    redistribution=(
        "Springer Nature supplementary artifact; local artifact not vendored"
    ),
    expected_sha256=IML1515_PUBLICATION_ARCHIVE_SHA256,
)
CHOE_2023_SUPPLEMENT_BUNDLE = SourceSpec(
    name="choe_2023_supplement_bundle",
    url=CHOE_2023_SUPPLEMENT_URL,
    filename="PMC9948719_supplementary_files.zip",
    source_version="Choe et al. 2023; DOI 10.1128/msystems.00896-22",
    redistribution="CC-BY-4.0 supplementary data",
)


def acquire_source(
    spec: SourceSpec,
    raw_dir: str | Path,
    *,
    refresh: bool = False,
    timeout_s: float = 60.0,
    retries: int = 3,
) -> tuple[Path, SourceRecord]:
    """Download *spec* atomically, or hash and reuse the local snapshot."""

    directory = Path(raw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / spec.filename
    reused = destination.exists() and not refresh
    if not reused:
        _download(spec.url, destination, timeout_s=timeout_s, retries=retries)
    accessed_at = datetime.now(UTC).isoformat()
    sha256 = file_sha256(destination)
    if spec.expected_sha256 is not None and sha256 != spec.expected_sha256:
        raise DataValidationError(
            f"{spec.name} content changed: expected {spec.expected_sha256}, got {sha256}; "
            "review and explicitly update the frozen artifact before use"
        )
    return destination, SourceRecord(
        name=spec.name,
        url=spec.url,
        local_path=str(destination),
        source_version=spec.source_version,
        accessed_at=accessed_at,
        sha256=sha256,
        bytes=destination.stat().st_size,
        cache_reused=reused,
        redistribution=spec.redistribution,
    )


def record_local_source(
    *,
    name: str,
    path: str | Path,
    source_version: str,
    source_url: str,
    redistribution: str,
) -> SourceRecord:
    """Record an explicitly supplied, already frozen local artifact."""

    source_path = Path(path)
    return SourceRecord(
        name=name,
        url=source_url,
        local_path=str(source_path),
        source_version=source_version,
        accessed_at=datetime.now(UTC).isoformat(),
        sha256=file_sha256(source_path),
        bytes=source_path.stat().st_size,
        cache_reused=True,
        redistribution=redistribution,
    )


def _download(url: str, destination: Path, *, timeout_s: float, retries: int) -> None:
    request = Request(
        url,
        headers={"User-Agent": "yggdrisil-ecoli/0.1 (+scientific data build)"},
    )
    last_error: OSError | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                with tempfile.NamedTemporaryFile(
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
            try:
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return
        except HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                raise
        except URLError as exc:
            last_error = OSError(str(exc))
        if attempt + 1 < retries:
            time.sleep(2**attempt)
    if last_error is None:
        raise OSError(f"failed to download {url}")
    raise OSError(f"failed to download {url} after {retries} attempts") from last_error
