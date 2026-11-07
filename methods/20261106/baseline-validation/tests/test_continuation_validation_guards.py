import hashlib,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.track_vecoli_experiment import solver_diagnostics,cell_files,simulation_environment
from yggdrisil_ecoli import vecoli


def test_closed_solver_records_preserved_and_strictly_checked(tmp_path):
    folder=tmp_path/'solver-diagnostics';folder.mkdir()
    data=b'p lp min 1 1 1\n';digest=hashlib.sha256(data).hexdigest()
    before=folder/'1.before.glp';after=folder/'1.after.glp';before.write_bytes(data);after.write_bytes(data)
    record={'model_unchanged':True,'return_code':0,'status':'GLP_OPT: optimal','original_lp_sha256':digest,'kkt':{str(i):{'relative_error':1e-8} for i in range(4)}}
    path=folder/'1.json';path.write_text(json.dumps(record))
    assert len(solver_diagnostics(tmp_path))==1
    task=SimpleNamespace(exit_code=None,workdir=tmp_path,variant_index=2,generation=1)
    assert cell_files(tmp_path,task,101)==[]
    task.exit_code=0
    assert {before,after,path}<=set(cell_files(tmp_path,task,101))
    record['kkt']['0']['relative_error']=1.01e-7;path.write_text(json.dumps(record))
    with pytest.raises(RuntimeError,match='Rejected solver'):solver_diagnostics(tmp_path)
    record['kkt']['0']['relative_error']=1e-8;path.write_text(json.dumps(record));after.write_bytes(b'changed objective')
    with pytest.raises(RuntimeError,match='Rejected solver'):solver_diagnostics(tmp_path)


def test_diagnostics_isolated_to_task_and_credentials_stripped():
    env=simulation_environment({'vecoli':{'checkout':'/native'},'execution':{'vecoli_python':'/native/.venv/bin/python','nextflow_executable':'/bin/nextflow'}},{'PATH':'/bin','WANDB_API_KEY':'tracking','OPENROUTER_API_KEY':'model','EDISON_PLATFORM_API_KEY':'edison'})
    assert env['YGGDRISIL_SOLVER_DIAGNOSTICS']=='solver-diagnostics'
    assert not any(x in env for x in ['WANDB_API_KEY','OPENROUTER_API_KEY','EDISON_PLATFORM_API_KEY'])


def test_exact_patch_allowlist_rejects_other_changes(monkeypatch,tmp_path):
    for name in ['pyproject.toml','uv.lock']:(tmp_path/name).write_text('x')
    (tmp_path/'.env').write_text('NXF_VER=25.10.4\n')
    monkeypatch.setattr(vecoli,'_git',lambda path,*args:vecoli.VECOLI_COMMIT if args[0]=='rev-parse' else 'M ecoli/processes/metabolism.py')
    with pytest.raises(ValueError,match='Unexpected tracked'):vecoli.validate_vecoli_checkout(tmp_path)
    monkeypatch.setattr(vecoli,'_git',lambda path,*args:vecoli.VECOLI_COMMIT if args[0]=='rev-parse' else 'M wholecell/utils/_netflow/nf_glpk.py')
    p=tmp_path/'wholecell/utils/_netflow/nf_glpk.py';p.parent.mkdir(parents=True);p.write_text('arbitrary patch')
    with pytest.raises(ValueError,match='Solver patch differs'):vecoli.validate_vecoli_checkout(tmp_path)
