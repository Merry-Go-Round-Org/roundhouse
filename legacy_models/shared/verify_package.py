"""Verify a real saved Python package in a fresh process; no fabricated data."""
import argparse,json,sys,tempfile
from pathlib import Path
import numpy as np,pandas as pd
from engine import score,checksum
def main(folder):
 pkg=Path(folder);manifest=json.loads((pkg/'manifest.json').read_text())
 if manifest['status']!='executed_full_data':raise ValueError('Not a trained package')
 if manifest['artifact_sha256']!=checksum(pkg/'model.joblib'):raise ValueError('Artifact hash mismatch')
 with tempfile.TemporaryDirectory() as temp:
  out=Path(temp)/'output.csv'
  score(pkg,pkg/'fixtures/development_inputs.csv',out)
  p=pd.read_csv(out,dtype={'row_id':str});q=pd.read_csv(pkg/'fixtures/reference_predictions.csv',dtype={'row_id':str})
  if not p.row_id.equals(q.row_id):raise ValueError('ID alignment mismatch')
  diff=np.abs(p.positive_probability.to_numpy()-q.positive_probability.to_numpy())
  if not np.isfinite(diff).all() or len(diff)==0 or diff.max()>1e-12:raise ValueError('Reproducibility tolerance exceeded')
  invalid=pd.read_csv(pkg/'fixtures/development_inputs.csv').drop(columns=['row_id'])
  bad=Path(temp)/'invalid.csv';invalid.to_csv(bad,index=False)
  try:score(pkg,bad,out)
  except ValueError:pass
  else:raise ValueError('Invalid input was accepted')
 print(json.dumps({'model':pkg.name,'passed':True,'n_rows':len(diff),'max_abs_difference':float(diff.max())}))
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--package',required=True);x=a.parse_args()
 try:main(x.package)
 except Exception as e:print('VERIFICATION FAILED:',repr(e),file=sys.stderr);sys.exit(1)
