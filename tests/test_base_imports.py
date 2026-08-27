import subprocess
import sys


def test_public_scorer_api_imports_without_openpyxl() -> None:
    script = """
import importlib.abc
import sys

class BlockOpenpyxl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "openpyxl" or fullname.startswith("openpyxl."):
            raise ModuleNotFoundError("openpyxl deliberately unavailable")
        return None

sys.meta_path.insert(0, BlockOpenpyxl())
import yggdrisil_ecoli.scorers
import yggdrisil_ecoli.tools
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
