"""UCI audit of saved independent Python model predictions, not training features."""
import argparse,json,sys
from pathlib import Path
import pandas as pd,numpy as np
from statsmodels.stats.proportion import proportion_confint
from engine import dataset,PATH
def main(package,data_path):
 pkg=Path(package);report=json.loads((pkg/'evaluation.json').read_text())
 if report['domain']!='uci':raise ValueError('Only UCI protected attributes available')
 full,sha=dataset('uci',data_path)
 if sha!=report['dataset_sha256']:raise ValueError('Data checksum mismatch')
 predictions=pd.read_csv(pkg.parent/'evidence/private_test'/(pkg.name+'_test_predictions.csv'),dtype={'row_id':str})
 if not predictions.row_id.is_unique:raise ValueError('Duplicate predictions')
 merged=predictions.merge(full[['row_id','SEX','AGE']],on='row_id',validate='one_to_one',indicator=True)
 if not (merged._merge=='both').all() or len(merged)!=len(predictions):raise ValueError('Audit alignment failure')
 merged['age_band']=pd.cut(merged.AGE,[-np.inf,29,44,59,np.inf],labels=['under_30','30_44','45_59','60_plus'])
 threshold=report['validation']['threshold_validation_f1']
 merged['flag']=(merged.positive_probability>=threshold)
 rows=[]
 for keys in [['SEX'],['age_band'],['SEX','age_band']]:
  for key,g in merged.groupby(keys,dropna=False,observed=True):
   if not isinstance(key,tuple):key=(key,)
   n=len(g);pos=int(g.label.sum());neg=n-pos
   tp=int(((g.label==1)&g.flag).sum());fp=int(((g.label==0)&g.flag).sum())
   def interval(k,d):return [float(v) for v in proportion_confint(k,d,method='wilson')] if d else None
   rows.append({'group':dict(zip(keys,[str(v) for v in key])),'n':n,'positives':pos,'negatives':neg,
    'positive_prediction_rate':float(g.flag.mean()),
    'positive_prediction_rate_ci95_wilson':interval(int(g.flag.sum()),n),
    'tpr':tp/pos if pos else None,'tpr_ci95_wilson':interval(tp,pos),
    'fpr':fp/neg if neg else None,'fpr_ci95_wilson':interval(fp,neg),
    'unstable':n<100 or min(pos,neg)<30})
 result={'description':'Descriptive historical SEX/AGE UCI group audit; not legal clearance','model':pkg.name,
   'dataset_sha256':sha,'source':'private frozen test predictions','rows':rows}
 (pkg/'fairness.json').write_text(json.dumps(result,indent=2))
 print(json.dumps({'model':pkg.name,'groups':len(rows),'status':'executed'}))
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--package',required=True);a.add_argument('--data',default=PATH['uci']);args=a.parse_args()
 try:main(args.package,args.data)
 except Exception as e:print('FAIRNESS AUDIT FAILED:',repr(e),file=sys.stderr);sys.exit(1)
