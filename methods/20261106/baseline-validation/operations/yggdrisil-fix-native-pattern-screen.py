import hashlib,json
from pathlib import Path
op=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-deletion-baselines-20gen-3seed-20261106')
p=op/'deployment/validation-application/src/yggdrisil_ecoli/baseline_storage.py'
text=p.read_text();a=text.index('def screen(');b=text.index('\n\nclass BaselineArchive',a)
policy_sha=hashlib.sha256((op/'credential-pattern-policy.json').read_bytes()).hexdigest()
replacement='''def screen(path: Path, secrets: list[bytes]) -> None:
    """Check every configured secret; resolve only evidenced protein-pattern collisions."""
    import mmap

    patterns = re.compile(rb"sk-or-v1-[a-fA-F0-9]{32,}|(?:AKIA|ASIA)[A-Z0-9]{16}|\\.platformv01\\.[A-Za-z0-9_-]{30,}")
    if path.stat().st_size == 0:
        return
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as content:
        if any(content.find(secret) >= 0 for secret in secrets):
            raise RuntimeError(f"Configured credential detected in checkpoint file: {path.name}")
        policy = None
        for match in patterns.finditer(content):
            if policy is None:
                policy_path = Path(__file__).resolve().parents[3] / "credential-pattern-policy.json"
                if file_sha256(policy_path) != "POLICY_SHA":
                    raise RuntimeError("Reviewed native protein policy changed")
                policy = json.loads(policy_path.read_bytes())["contexts"]
            candidates = policy.get(hashlib.sha256(match.group()).hexdigest(), [])
            verified_protein = False
            for candidate in candidates:
                start = match.start() - candidate["offset"]
                end = start + candidate["length"]
                if start >= 0 and end <= len(content) and hashlib.sha256(content[start:end]).hexdigest() == candidate["sha256"]:
                    verified_protein = True
                    break
            if not verified_protein:
                raise RuntimeError(f"Unreviewed credential pattern in checkpoint file: {path.name}")
'''.replace('POLICY_SHA',policy_sha)
p.write_text(text[:a]+replacement+text[b:])
p=op/'source/supervisor.py';text=p.read_text().replace("'__pycache__', '.nextflow', 'runscripts', 'ecoli', 'credentials'","'__pycache__', '.nextflow', 'runscripts', 'ecoli', 'credentials', 'source'")
text=text.replace("for label in ['runs', 'inputs', 'knockout-audit']:","for label in ['runs', 'inputs', 'knockout-audit', 'attempts']:")
text=text.replace("for directory, dirs, files in os.walk(root / label):","for directory, dirs, files in os.walk(root / label, onerror=walk_error):")
text=text.replace('def archive_paths(root):','def walk_error(error):\n    raise error\n\n\ndef archive_paths(root):')
text=text.replace("id='vecoli-baselines-study-20261106'","id='vecoli-baselines-study-20261106-retry1'")
p.write_text(text)
