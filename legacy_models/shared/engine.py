"""Legacy training and scoring: only verified UCI/ULB files, never downloads."""
import argparse, hashlib, json, sys
from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import (roc_auc_score, average_precision_score, brier_score_loss,
    log_loss, confusion_matrix, precision_score, recall_score, f1_score, precision_recall_curve, roc_curve)
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier

SEED=20261104
FEATURES={
 'uci':['LIMIT_BAL','EDUCATION','MARRIAGE']+['PAY_'+str(i) for i in [0,2,3,4,5,6]]+
    ['BILL_AMT'+str(i) for i in range(1,7)]+['PAY_AMT'+str(i) for i in range(1,7)],
 'ulb':['V'+str(i) for i in range(1,29)]+['Amount']}
TARGET={'uci':'default payment next month','ulb':'Class'}
CHECKSUM={'uci':'30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933',
 'ulb':'76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89'}
COUNTS={'uci':(30000,6636),'ulb':(284807,492)}
PATH={'uci':'datasets/uci/default of credit card clients.xls','ulb':'datasets/ulb/creditcard.csv'}

def checksum(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for block in iter(lambda:f.read(1048576),b''):h.update(block)
 return h.hexdigest()

def dataset(domain,path):
 path=Path(path)
 if not path.is_file():raise FileNotFoundError(f'Dataset absent: {path}; use existing download_datasets.py')
 sha=checksum(path)
 if sha!=CHECKSUM[domain]:raise ValueError(f'Dataset checksum differs: actual {sha}, expected {CHECKSUM[domain]}')
 if domain=='uci':
  try:
   data=pd.read_excel(path,sheet_name='Data',header=1)
  except ImportError:
   from xls_biff_reader import load_verified_uci
   data=load_verified_uci(path)
  if data.ID.isna().any() or data.ID.duplicated().any():raise ValueError('Invalid UCI ID')
  data.insert(0,'row_id',data.ID.astype(int).map(lambda v:f'uci-{v:06d}'))
 else:
  data=pd.read_csv(path)
  data.insert(0,'row_id',[f'ulb-{i:06d}' for i in range(len(data))])
 cols=FEATURES[domain]+[TARGET[domain]]
 if not set(cols).issubset(data.columns):raise ValueError('Required dataset columns absent')
 if (len(data),int(data[TARGET[domain]].sum()))!=COUNTS[domain]:raise ValueError('Unapproved dataset size/outcomes')
 if not set(data[TARGET[domain]].unique())=={0,1}:raise ValueError('Nonbinary labels')
 if not np.isfinite(data[cols].to_numpy(dtype=float)).all():raise ValueError('Missing or nonfinite data')
 return data,sha

def split(domain,data,root):
 # Immutable, SHA-verified, identical R/Python membership for canonical
 # fitted models. Fail closed on absent/mismatched UCI, auto-restore the
 # trivial time-contiguous ULB CSV with its pinned checksum.
 from pipeline import freeze_split
 return freeze_split(domain,data,root)

def classifier(algorithm):
 if algorithm=='logistic':
  return LogisticRegression(C=1,solver='lbfgs',max_iter=2000,random_state=SEED)
 if algorithm=='xgboost':
  return XGBClassifier(n_estimators=260,max_depth=3,learning_rate=.04,min_child_weight=8,
    subsample=.85,colsample_bytree=.85,reg_lambda=8,tree_method='hist',eval_metric='logloss',
    objective='binary:logistic',random_state=SEED,n_jobs=2)
 if algorithm=='lightgbm':
  return LGBMClassifier(n_estimators=260,max_depth=5,num_leaves=15,min_child_samples=70,
    learning_rate=.04,subsample=.85,subsample_freq=1,colsample_bytree=.85,
    reg_lambda=8,verbosity=-1,random_state=SEED,n_jobs=2)
 raise ValueError('Unrecognised algorithm')

def choose_threshold(y,p):
 precision,recall,thresholds=precision_recall_curve(y,p)
 f1=2*precision[:-1]*recall[:-1]/np.maximum(precision[:-1]+recall[:-1],1e-12)
 return float(thresholds[int(np.argmax(f1))])

def metric(y,p,threshold,domain,baseline):
 y=np.asarray(y,dtype=int);p=np.asarray(p,dtype=float)
 if len(y)!=len(p) or not len(y) or not np.isfinite(p).all() or not ((p>=0)&(p<=1)).all():
  raise ValueError('Invalid score vector')
 pred=p>=threshold
 tn,fp,fn,tp=map(int,confusion_matrix(y,pred,labels=[0,1]).ravel())
 auc=float(roc_auc_score(y,p)) if len(set(y))==2 else None
 out={'n':len(y),'positives':int(y.sum()),'positive_prevalence':float(y.mean()),
  'roc_auc':auc,'gini':None if auc is None else 2*auc-1,
  'pr_auc_definition':'sklearn.average_precision_score (noninterpolated average precision)',
  'pr_auc_average_precision':float(average_precision_score(y,p)),
  'log_loss':float(log_loss(y,p,labels=[0,1])),'brier':float(brier_score_loss(y,p)),
  'calibration_observed_rate':float(y.mean()),'calibration_expected_rate':float(p.mean()),
  'calibration_gap_observed_minus_expected':float(y.mean()-p.mean()),
  'baseline_train_prevalence':float(baseline),
  'baseline_brier':float(brier_score_loss(y,np.full(len(y),baseline))),
  'threshold_validation_f1':float(threshold),'confusion_matrix':{'tn':tn,'fp':fp,'fn':fn,'tp':tp},
  'precision':float(precision_score(y,pred,zero_division=0)),
  'recall':float(recall_score(y,pred,zero_division=0)),
  'specificity':float(tn/(tn+fp)) if tn+fp else None,
  'f1':float(f1_score(y,pred,zero_division=0))}
 if domain=='ulb':
  budget=max(1,int(np.ceil(.001*len(y))))
  top=np.argsort(-p,kind='stable')[:budget]
  out['alert_budget']={'rate':.001,'count':budget,'precision':float(y[top].mean()),
                      'recall':float(y[top].sum()/max(1,y.sum()))}
  fpr,tpr,_=roc_curve(y,p)
  out['recall_at_fpr_at_most_0_001']=float(tpr[fpr<=.001].max())
 return out

def writejson(path,obj):
 with open(path,'w',encoding='utf8') as f:json.dump(obj,f,indent=2,allow_nan=False)

def train(domain,algorithm,data_path,package):
 package=Path(package)
 data,sha=dataset(domain,data_path)
 evidence=package.parent/'evidence'
 partitions=split(domain,data,evidence)
 features=FEATURES[domain];y=data[TARGET[domain]].astype(int).to_numpy()
 X=data[features];tr=partitions=='train';val=partitions=='validation';test=partitions=='test'
 model=Pipeline([('imputer',SimpleImputer(strategy='median')),
  ('scaler',StandardScaler() if algorithm=='logistic' else 'passthrough'),
  ('estimator',classifier(algorithm))])
 model.fit(X.loc[tr],y[tr])
 val_p=model.predict_proba(X.loc[val])[:,1]
 threshold=choose_threshold(y[val],val_p)
 package.mkdir(parents=True,exist_ok=True)
 artifact=package/'model.joblib';joblib.dump(model,artifact)
 reloaded=joblib.load(artifact)
 if not np.allclose(val_p,reloaded.predict_proba(X.loc[val])[:,1],rtol=0,atol=1e-12):
  raise RuntimeError('Reload parity failed')
 test_p=reloaded.predict_proba(X.loc[test])[:,1]
 baseline=float(y[tr].mean())
 summary={'status':'executed_full_data','model_version':'1.0.0','domain':domain,'algorithm':algorithm,
  'dataset_sha256':sha,'seed':SEED,'split_file':str(evidence/(domain+'_split_ids.csv')),
  'split_sha256':checksum(evidence/(domain+'_split_ids.csv')),
  'validation':metric(y[val],val_p,threshold,domain,baseline),
  'test':metric(y[test],test_p,threshold,domain,baseline)}
 if domain=='ulb':summary['demographic_fairness']='not_assessable_no_protected_attributes'
 else:summary['demographic_fairness']='UCI SEX and AGE must be analysed independently with uncertainty; do not infer clearance'
 # Explicitly private test data, not to be exposed to migration agent.
 private=evidence/'private_test';private.mkdir(parents=True,exist_ok=True)
 pd.DataFrame({'row_id':data.loc[test,'row_id'],'label':y[test],'positive_probability':test_p}).to_csv(
   private/(package.name+'_test_predictions.csv'),index=False)
 fixtures=package/'fixtures';fixtures.mkdir(exist_ok=True)
 inputs=data.loc[val,['row_id']+features].head(100)
 inputs.to_csv(fixtures/'development_inputs.csv',index=False)
 pd.DataFrame({'row_id':inputs.row_id,'positive_probability':reloaded.predict_proba(inputs[features])[:,1]}).to_csv(
  fixtures/'reference_predictions.csv',index=False,float_format='%.17g')
 writejson(package/'evaluation.json',summary)
 manifest=json.loads((package/'manifest.json').read_text())
 manifest.update(status='executed_full_data',artifact_paths=['model.joblib'],
    artifact_sha256=checksum(artifact),dataset_sha256_observed_this_run=sha,
    metrics_reference='evaluation.json',frozen_split_sha256=checksum(evidence/(domain+'_split_ids.csv')),
    fixtures=['fixtures/development_inputs.csv','fixtures/reference_predictions.csv'])
 writejson(package/'manifest.json',manifest)
 print(json.dumps({'package':package.name,'validation':summary['validation']['pr_auc_average_precision'],
   'test':summary['test']['pr_auc_average_precision'],'artifact_sha256':checksum(artifact)}))

def score(package,input_path,output_path):
 package=Path(package);m=json.loads((package/'manifest.json').read_text())
 artifact=package/'model.joblib'
 if not artifact.is_file():raise FileNotFoundError(f'Missing trained artifact: {artifact}')
 if m.get('artifact_sha256')!=checksum(artifact):raise ValueError('Artifact checksum mismatch')
 df=pd.read_csv(input_path,dtype={'row_id':str})
 if not len(df) or df.columns.duplicated().any() or 'row_id' not in df or df.row_id.isna().any() or df.row_id.duplicated().any() or (df.row_id.str.strip()=='').any():
  raise ValueError('Nonempty unique row_id is required')
 if any(c in df for c in ['Class','default payment next month']):raise ValueError('Target labels prohibited in input')
 columns=m['feature_order']
 if not set(columns).issubset(df.columns):raise ValueError('Required features absent')
 X=df[columns].apply(pd.to_numeric,errors='raise')
 if not np.isfinite(X.to_numpy(dtype=float)).all():raise ValueError('Nonfinite input')
 # Never load untrusted joblib/pickle, even if checksum matches an untrusted manifest.
 p=joblib.load(artifact).predict_proba(X)[:,1]
 if not np.isfinite(p).all() or ((p<0)|(p>1)).any():raise ValueError('Invalid probabilities')
 pd.DataFrame({'row_id':df.row_id,'positive_probability':p}).to_csv(
     output_path,index=False,float_format='%.17g')
