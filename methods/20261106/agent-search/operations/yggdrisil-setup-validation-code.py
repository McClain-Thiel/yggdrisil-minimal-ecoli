import hashlib,json,shutil,subprocess
from pathlib import Path
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
subprocess.run([str(r/'env/bin/python'),str(r/'yggdrisil-prepare-validation-source.py')],check=True)
v=r/'validation';v.mkdir(exist_ok=True);shutil.copy2(r/'audit-knockouts.py',v/'audit-knockouts.py')
n=r/'native-validated'
assert not n.exists()
subprocess.run(['git','clone','--shared',str(r/'native'),str(n)],check=True)
(n/'.venv').symlink_to(r/'native/.venv',target_is_directory=True)
patch=r/'solver-recovery-v2.patch'
assert hashlib.sha256(patch.read_bytes()).hexdigest()=='3a4a50fdc7b4d16277b7ce8d3cb76e2e2fd69132937a4d57cbb232b01a06592a'
subprocess.run(['git','apply',str(patch)],cwd=n,check=True)
assert hashlib.sha256((n/'wholecell/utils/_netflow/nf_glpk.py').read_bytes()).hexdigest()=='5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686'
print('Exact solver patch and isolated validation code prepared')
