"""Retrieve only allowlisted compact worker records, never scientific graphs."""
import json
import subprocess
from pathlib import Path

folder = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/deletion-baselines-20261106')
names = ['launcher.json', 'preflight.json', 'input-verification.json',
         'metadata-repair.json', 'monitoring-summary.json', 'batch-progress.json',
         'startup-attempt-audit.json',
         'attempts/package-metadata-before-search/final-backup.json',
         'attempts/package-metadata-before-search/launcher.json']
source = ('import json\nfrom pathlib import Path\n'
          'r=Path("/home/ubuntu/yggdrisil-baselines-20261106")\n'
          f'names={names!r}\n'
          'print(json.dumps({n:json.loads((r/n).read_text()) for n in names}))\n')
worker = json.loads((folder / 'worker-launch.json').read_text())
command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'ClearAllForwardings=yes', '-o', 'HostKeyAlias=3.87.218.59',
           '-i', str(Path.home() / '.ssh/mcclain-phd-aws-dev.pem'),
           'ubuntu@' + worker['public_ip'], 'python3 -']
result = subprocess.run(command, input=source, text=True, check=True, capture_output=True)
records = json.loads(result.stdout)
for name, value in records.items():
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
summary = records['monitoring-summary.json']
stamp = summary['checked_at'].replace('-', '').replace(':', '').split('.')[0]
(folder / f'monitoring-{stamp}.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps({'checked_at': summary['checked_at'], 'status': summary['batch']['status'],
                  'verified_checkpoints': sum(j.get('checkpoint_independently_verified', False) for j in summary['jobs'])}))
