"""Append post-fit descriptive bootstrap and calibration-bin audit to existing real evaluation."""
import argparse,json,sys
from pathlib import Path
import numpy as np,pandas as pd
from sklearn.metrics import average_precision_score,roc_auc_score
from engine import dataset,PATH,TARGET

def run(pkg,dataset_path):
 pkg=Path(pkg)
 summary=json.loads((pkg/'evaluation.json').read_text())
 domain=summary['domain'] if 'domain' in summary else ('uci' if pkg.name.startswith('credit_') else 'ulb')
 frame,sha=dataset(domain,dataset_path)
 if summary['dataset_sha256']!=sha:raise ValueError('Checksum mismatch')
 train_file=pkg.parent/'evidence'/(domain+'_split_ids.csv')
 split=pd.read_csv(train_file,dtype=str)
 if not split.row_id.equals(frame.row_id):raise ValueError('Frozen row_id mismatch')
 predicted=pd.read_csv(pkg.parent/'evidence/private_test'/(pkg.name+'_test_predictions.csv'),dtype={'row_id':str})
 if not predicted.row_id.is_unique:raise ValueError('Duplicate row_id in private predictions')
 test=frame.loc[split['split']=='test',['row_id',TARGET[domain]]].reset_index(drop=True)
 if not test.row_id.equals(predicted.row_id):raise ValueError('Test row ordering changed')
 y=test[TARGET[domain]].to_numpy(dtype=int);p=predicted.positive_probability.to_numpy(dtype=float)
 if not np.isfinite(p).all() or (p<0).any() or (p>1).any() or not np.array_equal(y,predicted.label.to_numpy(dtype=int)):
  raise ValueError('Labels or scores differ')
 rng=np.random.default_rng(20261104);aps=[];aucs=[]
 for _ in range(250):
  ix=rng.integers(0,len(y),len(y))
  if len(np.unique(y[ix]))<2:continue
  aps.append(float(average_precision_score(y[ix],p[ix])))
  aucs.append(float(roc_auc_score(y[ix],p[ix])))
 def ci(values):return list(map(float,np.quantile(values,[.025,.975]))) if len(values)>=30 else None
 summary['test']['uncertainty']={'method':'iid_bootstrap_percentile_95',
  'resamples_requested':250,'valid_resamples':len(aps),'pr_auc_ci95':ci(aps),'roc_auc_ci95':ci(aucs),
  'caveat':'Assumes independent rows; not evidence of account-level independence or long-term performance'}
 # Predeclared equal-width bins; avoid fitting bins on final test outcomes.
 edges=np.linspace(0,1,11);indices=np.minimum(np.searchsorted(edges,p,side='right')-1,9)
 bins=[]
 for i in range(10):
  m=indices==i
  bins.append({'bin':i,'lower':float(edges[i]),'upper':float(edges[i+1]),
   'n':int(m.sum()),'events':int(y[m].sum()),'mean_prediction':float(p[m].mean()) if m.any() else None,
   'observed_event_rate':float(y[m].mean()) if m.any() else None})
 summary['test']['calibration_fixed_probability_bins']=bins
 summary['test']['calibration_bins_policy']='Fixed 0.1-wide probability bins established before test; empty bins not assessable'
 (pkg/'evaluation.json').write_text(json.dumps(summary,indent=2,allow_nan=False))
 print(json.dumps({'model':pkg.name,'updated':'bootstrap_and_fixed_calibration_bins','resamples':len(aps)}))
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--package',required=True);a.add_argument('--data');v=a.parse_args()
 domain='uci' if Path(v.package).name.startswith('credit_') else 'ulb'
 try:run(v.package,v.data or PATH[domain])
 except Exception as e:print('DIAGNOSTICS ERROR:',repr(e),file=sys.stderr);sys.exit(1)
