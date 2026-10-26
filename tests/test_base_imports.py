import subprocess
import sys


def test_public_scorer_api_imports_without_data_preparation_libraries() -> None:
    script = """
import importlib.abc
import sys

class BlockPreparationLibraries(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'openpyxl', 'gffutils', 'pooch', 'pysam', 'pypdf', 'pydantic_ai', 'dotenv', 'rba', 'rbatools', 'swiglpk'}:
            raise ModuleNotFoundError(f"{fullname} deliberately unavailable")
        return None

sys.meta_path.insert(0, BlockPreparationLibraries())
from yggdrisil_ecoli.data.evidence import load_genes
import yggdrisil_ecoli.scorers
from yggdrisil_ecoli.scorers.rba import RBAScorer
import yggdrisil_ecoli.tools
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
