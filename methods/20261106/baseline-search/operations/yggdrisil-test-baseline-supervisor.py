import hashlib,importlib.util,json,shutil,sqlite3,tempfile
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('baseline_supervisor',Path(__file__).with_name('yggdrisil-baseline-supervisor.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
for corrupt in [False,True]:
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory);bundle=root/'bundle';bundle.mkdir();m.ROOT=root;m.BUNDLE=bundle;m.SPEC={'experiment_id':'fixture'};m.JOBS=[{'directory':'search/run'}];m.CHILDREN={};m.collect=lambda b:{'status':'fixture'}
  for n in ['protocol.json','manifest.json','source-manifest.json','jobs.json','inputs/starting-genotype.json','catalog.json','approval.json']:
   p=bundle/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
  for n in ['batch-progress.json','preflight.json','input-verification.json','worker.json']:(root/n).write_text('{}')
  folder=bundle/'search/run';folder.mkdir(parents=True);connection=sqlite3.connect(folder/'graph.sqlite');connection.execute('pragma journal_mode=wal');connection.execute('create table evidence(value integer)');connection.execute('insert into evidence values(17)');connection.commit()
  (bundle/'source').mkdir();(bundle/'source/private.py').write_text('SOURCE_CANARY');(root/'credentials').mkdir();(root/'credentials/key.json').write_text('SECRET_CANARY')
  storage={};artifacts={}
  class Archive:
   def upload(self,p,category):
    data=p.read_bytes();digest=hashlib.sha256(data).hexdigest();storage[p.name]=data;return {'uri':'s3://fixture/'+digest,'sha256':digest,'bytes':len(data)}
  class Artifact:
   def __init__(self,name,type):self.paths={};self.qualified_name='fixture:v0';self.state='COMMITTED'
   def add_file(self,path,name):self.paths[name]=Path(path)
   def wait(self):return None
   def download(self,root):
    for n,p in self.paths.items():shutil.copyfile(p,Path(root)/n)
    if corrupt:(Path(root)/'data.tar.gz').write_bytes(b'corrupt')
    return root
  def log(a):artifacts['value']=a;return a
  wandb=SimpleNamespace(Artifact=Artifact,Api=lambda timeout:SimpleNamespace(artifact=lambda v:artifacts['value']))
  try:result=m.final_backup({'status':'fixture'},Archive(),[b'SECRET_CANARY'],SimpleNamespace(log_artifact=log),wandb)
  except AssertionError:
   assert corrupt
   assert not (root/'final-backup.json').exists()
  else:
   assert not corrupt and result['status']=='verified'
   import io,tarfile
   with tarfile.open(fileobj=io.BytesIO(storage['data.tar.gz'])) as tar:
    assert all('source/' not in n and 'credentials/' not in n for n in tar.getnames())
    db=root/'restored.sqlite';db.write_bytes(tar.extractfile('search/run/graph.sqlite').read());restored=sqlite3.connect(db);assert restored.execute('select * from evidence').fetchall()==[(17,)];restored.close()
  connection.close()
print('2 supervisor archival checks passed: live-WAL rows preserved with source/credential exclusions; corrupted downloaded final artifact rejected.')
