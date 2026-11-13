"""Evaluate a genuinely trained R GLM using the same frozen split, without retuning on test."""
import argparse,json,subprocess,sys,tempfile
from pathlib import Path
import numpy as np,pandas as pd
from engine import dataset,split,metric,PATH,TARGET,checksum
def evaluate(domain,package,dataset_path):
 pkg=Path(package).resolve()
 manifest=json.loads((pkg/'manifest.json').read_text())
 artifact=pkg/'model.rds'
 if not artifact.is_file() or manifest.get('artifact_sha256')!=checksum(artifact):
  raise ValueError('R artifact missing, untrusted or hash mismatch')
 if manifest.get('status')!='executed_full_data':raise ValueError('R model not trained')
 data,sha=dataset(domain,dataset_path)
 parts=split(domain,data,pkg.parent/'evidence')
 feats=manifest['feature_order'];labels=data[TARGET[domain]].astype(int).to_numpy()
 train=parts=='train';val=parts=='validation';test=parts=='test'
 def run(rows):
  with tempfile.TemporaryDirectory() as root:
   inputs=Path(root)/'inputs.csv';outputs=Path(root)/'outputs.csv'
   data.loc[rows,['row_id']+feats].to_csv(inputs,index=False)
   subprocess.run(['Rscript',str(pkg/'score.R'),'--package',str(pkg),'--input',str(inputs),
                   '--output',str(outputs)],check=True)
   pred=pd.read_csv(outputs,dtype={'row_id':str})
   if not pred.row_id.equals(data.loc[rows,'row_id'].reset_index(drop=True)):
    raise ValueError('R scoring reorders row IDs')
   return pred.positive_probability.to_numpy()
 val_p=run(val);test_p=run(test)
 from engine import choose_threshold
 threshold=choose_threshold(labels[val],val_p)
 base=float(labels[train].mean())
 report={'status':'executed_full_data','domain':domain,'model_version':'1.0.0',
  'dataset_sha256':sha,'split_reference':manifest['split_reference'],
  'validation':metric(labels[val],val_p,threshold,domain,base),
  'test':metric(labels[test],test_p,threshold,domain,base),
  'protected_group_fairness':'not_assessable' if domain=='ulb' else 'Run uci_fairness.py for disaggregated audit'}
 private=pkg.parent/'evidence/private_test';private.mkdir(parents=True,exist_ok=True)
 pd.DataFrame({'row_id':data.loc[test,'row_id'],'label':labels[test],'positive_probability':test_p}).to_csv(
  private/(pkg.name+'_test_predictions.csv'),index=False)
 (pkg/'evaluation.json').write_text(json.dumps(report,indent=2))
 manifest['metrics_reference']='evaluation.json'
 (pkg/'manifest.json').write_text(json.dumps(manifest,indent=2))
 print(json.dumps({'model':pkg.name,'validation_average_precision':report['validation']['pr_auc_average_precision'],
                   'test_average_precision':report['test']['pr_auc_average_precision']}))
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--domain',choices=['uci','ulb'],required=True)
 a.add_argument('--package',required=True);a.add_argument('--data');v=a.parse_args()
 try:evaluate(v.domain,v.package,v.data or PATH[v.domain])
 except Exception as e:print('R MODEL EVALUATION FAILED:',repr(e),file=sys.stderr);sys.exit(1)
