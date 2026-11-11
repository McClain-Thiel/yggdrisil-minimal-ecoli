from pathlib import Path
p=Path('/home/ubuntu/yggdrisil-continuation-20261106/validation-application/scripts/track_vecoli_experiment.py')
s=p.read_text()
old='    secret = os.environ.get("WANDB_API_KEY", "").encode()'
new='''    credentials = Path("/home/ubuntu/yggdrisil-continuation-20261106/credentials")
    secrets = [value.encode() for path in credentials.glob("*.json")
               for key, value in json.loads(path.read_text()).items()
               if key.endswith("_KEY") and isinstance(value, str)]
    if len(secrets) != 4:
        raise RuntimeError("Expected all four configured run secrets for backup screening")'''
assert old in s;s=s.replace(old,new).replace('        if secret and secret in path.read_bytes():','        if any(secret in path.read_bytes() for secret in secrets):')
compile(s,str(p),'exec');p.write_text(s)
print('Validation backups screen all configured run secrets')
