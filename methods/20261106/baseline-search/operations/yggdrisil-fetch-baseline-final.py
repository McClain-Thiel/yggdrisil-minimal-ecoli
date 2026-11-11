import json
import subprocess
from pathlib import Path

out = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/deletion-baselines-20261106/final')
names = {'analysis.json': 'final-verification/analysis.json',
         'audit.json': 'final-verification/audit.json',
         'nominations.json': 'final-verification/nominations.json',
         'completion-record-publication.json': 'final-verification/publication.json',
         'final-backup.json': 'final-backup.json', 'batch-progress.json': 'batch-progress.json'}
source = ('import json\nfrom pathlib import Path\n'
          'r=Path("/home/ubuntu/yggdrisil-baselines-20261106")\n'
          f'names={names!r}\n'
          'print(json.dumps({k:json.loads((r/v).read_text()) for k,v in names.items()}))\n')
command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'ClearAllForwardings=yes', '-o', 'HostKeyAlias=3.87.218.59',
           '-i', str(Path.home() / '.ssh/mcclain-phd-aws-dev.pem'),
           'ubuntu@18.233.169.166', 'python3 -']
result = subprocess.run(command, input=source, text=True, capture_output=True, check=True)
for name, value in json.loads(result.stdout).items():
    (out / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
print('Saved six compact final records.')
