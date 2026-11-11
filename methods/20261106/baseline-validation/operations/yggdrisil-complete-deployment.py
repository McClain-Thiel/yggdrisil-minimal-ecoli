import hashlib,json,subprocess
from pathlib import Path
repo=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');op=repo/'runs/vecoli-deletion-baselines-20gen-3seed-20261106'
ssh=['ssh','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','ubuntu@52.87.179.109']
root='/home/ubuntu/yggdrisil-baseline-validation-20261106'
files={str(p.relative_to(op/'deployment/payload')):p for p in (op/'deployment/payload').rglob('*') if p.is_file()}
for name in ['preflight.py','test_launch_guards.py','finish-runtime.sh']:
    path=op/'source'/name;files[name]=path
    subprocess.run(ssh+['cat > '+root+'/'+name],input=path.read_bytes(),check=True)
manifest={'files':{name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in sorted(files.items())},'scientific_inputs_unchanged':True,'operational_changes':'Same pinned v2 solver patch, full changed-file W&B downloads, full secret scan and bounded supervisor. All exact genotypes frozen before results.'}
p=op/'deployment-manifest.json';p.write_text(json.dumps(manifest,indent=2)+'\n')
subprocess.run(ssh+['cat > '+root+'/deployment-manifest.json'],input=p.read_bytes(),check=True)
cmd='cd '+root+' && export PYTHONPATH='+root+'/application/src:'+root+'/application:'+root+'/framework/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__; '+root+'/env/bin/python -m pytest test_launch_guards.py -q && '+root+'/env/bin/python preflight.py'
subprocess.run(ssh+[cmd],check=True)
