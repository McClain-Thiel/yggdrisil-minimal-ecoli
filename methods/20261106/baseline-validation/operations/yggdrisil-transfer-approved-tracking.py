import json,netrc,subprocess
from pathlib import Path
_,_,key=netrc.netrc(Path.home()/'.netrc').authenticators('api.wandb.ai')
assert key
root='/home/ubuntu/yggdrisil-baseline-validation-20261106'
ssh=['ssh','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','ubuntu@52.87.179.109']
command='umask 077; mkdir -p '+root+'/credentials; cat > '+root+'/credentials/tracking.json; chmod 700 '+root+'/credentials; chmod 600 '+root+'/credentials/tracking.json'
subprocess.run(ssh+[command],input=json.dumps({'WANDB_API_KEY':key}).encode(),check=True)
print('Only W&B tracking login transferred; credential directory 0700 and file 0600.')
