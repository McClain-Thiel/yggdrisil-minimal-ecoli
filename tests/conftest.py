from pathlib import Path

import pandas as pd
import pytest

from yggdrisil_ecoli.data.evidence import validate_genes
from yggdrisil_ecoli.data.gff import parse_ncbi_gff


@pytest.fixture
def genes() -> pd.DataFrame:
    frame, _ = parse_ncbi_gff(Path(__file__).parent / "fixtures/mg1655_excerpt.gff3")
    return validate_genes(
        frame.assign(lb_call_raw="NE", lb_ecipkm=3.0, m9_call_raw="NE", m9_ecipkm=3.0)
    )
