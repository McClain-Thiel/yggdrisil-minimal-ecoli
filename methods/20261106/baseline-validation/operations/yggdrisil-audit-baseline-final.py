import ast
import gzip
import hashlib
import io
import json
import math
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import boto3
import pyarrow.parquet as pq

OUT = Path('/private/tmp/yggdrisil-baseline-final-audit')
ROOT = '/home/ubuntu/yggdrisil-baseline-validation-20261106/'
read = lambda p: json.loads(p.read_bytes())
now = lambda: datetime.now(timezone.utc).isoformat()
index = read(OUT / 'final-index.json')
expected = {f['path']: f for f in index['files']}
assert len(expected) == len(index['files'])
ref = index['archive']
s3 = boto3.client('s3', region_name='us-east-1')
owner = boto3.client('sts').get_caller_identity()['Account']
metadata = OUT / 'metadata'
metadata.mkdir(exist_ok=True)
columns = ['global_time', 'listeners__mass__cell_mass', 'listeners__mass__dry_mass', 'listeners__mass__dry_mass_fold_change']
histories, daughters, errors, diagnostics = {}, {}, {}, {}
manifests = {}

class HashReader:
    def __init__(self, body):
        self.body, self.digest, self.count = body, hashlib.sha256(), 0
    def read(self, size=-1):
        data = self.body.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data

def retain(name, data):
    target = metadata / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

u = urlparse(ref['uri'])
response = s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'), ExpectedBucketOwner=owner)
assert response['ContentLength'] == ref['bytes']
assert response['ServerSideEncryption'] == 'AES256'
body = response['Body']
stream = HashReader(body)
seen = set()
total = 0
with gzip.GzipFile(fileobj=stream) as uncompressed:
    with tarfile.open(fileobj=uncompressed, mode='r|') as archive:
        for member in archive:
            name = member.name
            path = PurePosixPath(name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts, name
            assert name in expected and name not in seen, name
            assert path.suffix not in {'.py', '.pyc', '.sh', '.nf', '.patch', '.diff'}, name
            assert not set(path.parts) & {'credentials', 'source', '.env', '.netrc'}, name
            entry = expected[name]
            assert member.size == entry['bytes'], name
            current = name.startswith('runs/') and len(path.parts) >= 3
            label = path.parts[1] if current else None
            compact = (len(path.parts) == 1 and path.suffix == '.json') or (current and len(path.parts) == 3 and path.suffix == '.json') or name == 'knockout-audit/variant-knockout-audit.json'
            daughter = current and '/daughter_states/' in name and path.name.startswith('daughter_state_')
            history = current and '/history/' in name and path.suffix == '.pq'
            stderr = current and path.name == '.command.err'
            diagnostic = current and '/solver-diagnostics/' in name and path.suffix == '.json'
            keep = compact or daughter or history or stderr or diagnostic
            chunks = []
            digest, count = hashlib.sha256(), 0
            with archive.extractfile(member) as content:
                while chunk := content.read(1024**2):
                    digest.update(chunk)
                    count += len(chunk)
                    if keep:
                        chunks.append(chunk)
            assert count == member.size and digest.hexdigest() == entry['sha256'], name
            seen.add(name)
            total += count
            if keep:
                data = b''.join(chunks)
            if compact:
                retain(name, data)
                if current and path.name == 'manifest.json':
                    manifests[label] = json.loads(data)
            if daughter:
                variant = int(re.search(r'/variant=(\d+)/', name)[1])
                spec = next(s for s in manifests[label]['finalists'] if s['variant_index'] == variant)
                targets = set(spec['target_rna_indices'])
                agents = json.loads(data)['agents']
                assert len(agents) == 1, name
                state = next(iter(agents.values()))
                fields = [f[0] for f in ast.literal_eval(state['unique_dtypes']['RNA'])]
                tu, active = fields.index('TU_index'), fields.index('_entryState')
                assert not any(row[active] and row[tu] in targets for row in state['unique']['RNA']), name
                daughters[name] = {'sha256': entry['sha256'], 'bytes': count, 'target_rna_count': len(targets), 'target_transcripts_absent': True}
            if history:
                rows = pq.ParquetFile(io.BytesIO(data)).read(columns=columns).to_pylist()
                assert rows, name
                for row in rows:
                    for key, value in row.items():
                        if key == 'global_time' and value is None:
                            continue
                        assert value is not None and math.isfinite(value), (name, key)
                histories[name] = {'sha256': entry['sha256'], 'rows': len(rows), 'first': rows[0], 'last': rows[-1], 'finite_mass_time': True}
            if stderr:
                text = data.decode('utf-8')
                messages = [line.strip() for line in text.splitlines() if any(t in line for t in ['Warning:', 'RuntimeError:', 'TimeLimitError', 'InvalidBoundaryError:', 'Error:', 'Exception:'])]
                errors[name] = {'sha256': entry['sha256'], 'bytes': count, 'messages': messages, 'tail': text[-3000:]}
            if diagnostic:
                diagnostics[name] = {'sha256': entry['sha256'], 'data': json.loads(data)}
            if len(seen) % 200 == 0:
                print(json.dumps({'members': len(seen), 'member_bytes': total, 'compressed_bytes': stream.count}), flush=True)
    while uncompressed.read(1024**2):
        pass
while stream.read(1024**2):
    pass
body.close()
assert seen == set(expected)
assert stream.count == ref['bytes'] and stream.digest.hexdigest() == ref['sha256']
record = {'status': 'verified', 'verified_at': now(), 'archive': ref, 'index_sha256': hashlib.sha256((OUT/'final-index.json').read_bytes()).hexdigest(), 'members': len(seen), 'member_bytes': total, 'all_names_sizes_sha256_verified': True, 'full_s3_get_sha256_verified': True, 'aes256': True, 'source_and_credential_paths_excluded': True, 'histories': histories, 'daughters': daughters, 'errors': errors, 'solver_diagnostics': diagnostics, 'worker_comparison': 'Worker is stopped. Closed outputs will be cross-checked against independent final checkpoint inventories and W&B downloads before deletion.'}
(OUT / 'archive-member-audit.json').write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps({'status': 'verified', 'members': len(seen), 'member_bytes': total, 'histories': len(histories), 'daughters': len(daughters), 'solver_diagnostics': len(diagnostics)}), flush=True)
