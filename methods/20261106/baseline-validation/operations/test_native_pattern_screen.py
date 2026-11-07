import hashlib,json,re
from pathlib import Path
import pytest
from yggdrisil_ecoli.baseline_storage import screen
ROOT=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')


def test_all_native_protein_collisions_pass_full_context():
    screen(ROOT/'inputs/simData.cPickle',[])


def test_actual_configured_secret_always_rejected_even_in_protein(tmp_path):
    content=(ROOT/'inputs/simData.cPickle').read_bytes()
    token=re.search(rb'(?:AKIA|ASIA)[A-Z0-9]{16}',content).group()
    with pytest.raises(RuntimeError,match='Configured credential'):
        screen(ROOT/'inputs/simData.cPickle',[token])


def test_known_protein_substring_without_full_protein_rejected(tmp_path):
    content=(ROOT/'inputs/simData.cPickle').read_bytes();token=re.search(rb'(?:AKIA|ASIA)[A-Z0-9]{16}',content).group()
    path=tmp_path/'bad.json';path.write_bytes(b'{"key":"'+token+b'"}')
    with pytest.raises(RuntimeError,match='Unreviewed credential pattern'):screen(path,[])


def test_unknown_key_and_chunk_boundary_secret_rejected(tmp_path):
    path=tmp_path/'bad.bin';path.write_bytes(b'x'*(1024*1024-5)+b'AKIA0123456789ABCDEF')
    with pytest.raises(RuntimeError,match='Unreviewed credential pattern'):screen(path,[])
    path.write_bytes(b'x'*(1024*1024-5)+b'tracking-secret-value')
    with pytest.raises(RuntimeError,match='Configured credential'):screen(path,[b'tracking-secret-value'])
