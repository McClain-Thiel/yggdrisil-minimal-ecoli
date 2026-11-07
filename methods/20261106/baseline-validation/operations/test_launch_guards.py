import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from scripts.track_vecoli_experiment import verify_wandb_files


def test_wandb_rejects_mismatch_and_uncommitted(monkeypatch, tmp_path):
    path=tmp_path/'progress.json'
    path.write_bytes(b'actual')
    remote=SimpleNamespace(state='COMMITTED',get_entry=lambda name:SimpleNamespace(download=lambda root:str(path)))
    monkeypatch.setitem(sys.modules,'wandb',SimpleNamespace(Api=lambda **kw:SimpleNamespace(artifact=lambda name:remote)))
    artifact=SimpleNamespace(qualified_name='project/test:v0')
    verify_wandb_files(artifact,{'progress.json':hashlib.sha256(b'actual').hexdigest()},tmp_path)
    with pytest.raises(RuntimeError,match='round-trip mismatch'):
        verify_wandb_files(artifact,{'progress.json':hashlib.sha256(b'different').hexdigest()},tmp_path)
    remote.state='PENDING'
    with pytest.raises(RuntimeError,match='not committed'):
        verify_wandb_files(artifact,{},tmp_path)


def test_final_archive_excludes_source_credentials_and_open_caches(tmp_path):
    spec=importlib.util.spec_from_file_location('baseline_supervisor',Path(__file__).with_name('supervisor.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep=['batch-progress.json','runs/calibration/progress.json','runs/calibration/work/a/.command.err','runs/calibration/work/a/solver-diagnostics/test.glp']
    exclude=['credentials/tracking.json','application/secret.json','runs/calibration/runscripts/source.py','runs/calibration/wandb/settings.json','runs/calibration/work/a/.command.sh','runs/calibration/.nextflow/cache/x.json']
    for name in keep+exclude:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('x')
    assert {str(p.relative_to(tmp_path)) for p in module.archive_paths(tmp_path)}==set(keep)
