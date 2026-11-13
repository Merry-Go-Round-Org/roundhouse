"""Shared, auditable full-data training utilities; no network activity."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (average_precision_score, brier_score_loss, confusion_matrix,
                             f1_score, log_loss, precision_score, recall_score, roc_auc_score)
from sklearn.model_selection import GroupShuffleSplit
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
import joblib

SEED = 20261104
UCI_COLUMNS = ['LIMIT_BAL','EDUCATION','MARRIAGE'] + [f'PAY_{i}' for i in (0,2,3,4,5,6)] + [f'BILL_AMT{i}' for i in range(1,7)] + [f'PAY_AMT{i}' for i in range(1,7)]
ULB_COLUMNS = [f'V{i}' for i in range(1,29)] + ['Amount']
DATA = {
    'uci': {'path':'datasets/uci/default of credit card clients.xls','sha256':'30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933','target':'default payment next month','expected':(30000,6636), 'features':UCI_COLUMNS},
    'ulb': {'path':'datasets/ulb/creditcard.csv','sha256':'76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89','target':'Class','expected':(284807,492), 'features':ULB_COLUMNS}
}


def sha256(path):
    dig=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            dig.update(block)
    return dig.hexdigest()


def read_data(domain, filename, verify=True):
    spec=DATA[domain]
    filename=Path(filename)
    if not filename.is_file():
        raise FileNotFoundError(f'Dataset missing: {filename}; run python download_datasets.py uci ulb from repo root')
    checksum=sha256(filename)
    if verify and checksum!=spec['sha256']:
        raise ValueError(f'Unapproved dataset checksum {checksum}, expected {spec["sha256"]}')
    if domain=='uci':
        try:
            import xlrd  # standard upstream loader if available
            df=pd.read_excel(filename,sheet_name='Data',header=1)
        except ImportError:
            from xls_biff_reader import load_verified_uci
            df=load_verified_uci(filename)
        if 'ID' not in df.columns:raise ValueError('UCI ID column is missing')
        if df['ID'].isna().any() or df['ID'].duplicated().any():raise ValueError('UCI ID not unique')
        df.insert(0,'row_id',df['ID'].astype('int64').map(lambda x:f'uci-{x:06d}'))
    else:
        df=pd.read_csv(filename)
        df.insert(0,'row_id', [f'ulb-{i:06d}' for i in range(len(df))])
    required=spec['features']+[spec['target']]
    if not set(required).issubset(df.columns):raise ValueError(f'Missing expected columns: {sorted(set(required)-set(df.columns))}')
    if len(df)!=spec['expected'][0] or int((df[spec['target']]==1).sum())!=spec['expected'][1]:
        raise ValueError('Dataset count/positives differ from frozen benchmark')
    if not set(df[spec['target']].dropna().unique())=={0,1}:raise ValueError('Target must be binary 0/1')
    if df[required].isna().any().any():raise ValueError('Missing raw feature or outcome values: abort, review provenance')
    if not np.isfinite(df[required].to_numpy(dtype=float)).all():raise ValueError('Non-finite raw data')
    return df,checksum


def freeze_split(domain, df, root):
    """Read SHA-pinned canonical splits; never silently regenerate holdout rows.

    Current UCI models used an integer-feature hash. The original float BIFF
    reader historical split is retained separately, NEVER used by current fits.
    """
    root = Path(root)
    path = root / f'{domain}_split_ids.csv'
    expected_hash = {
        'uci': 'e8feb21a0c57c5d6c8344af11aaaab079064f259e4861faad8f4aef40fe6d6cb',
        'ulb': '5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa',
    }[domain]
    if not path.is_file() and domain == 'ulb':
        from restore_ulb_split import restore
        restore(path)
    if not path.is_file():
        raise FileNotFoundError(f'Frozen split missing: {path}; check out the committed canonical UCI CSV')
    if sha256(path) != expected_hash:
        raise ValueError(f'Frozen split SHA differs from canonical {domain} split. Do not overwrite: {path}')
    frozen = pd.read_csv(path, dtype=str)
    if list(frozen.columns) != ['row_id', 'split'] or len(frozen) != len(df):
        raise ValueError('Invalid split schema or row count')
    if not frozen.row_id.equals(df.row_id.astype(str).reset_index(drop=True)):
        raise ValueError('Frozen row IDs/order mismatch original dataset')
    if frozen.isna().any().any() or set(frozen.split.unique()) != {'train', 'validation', 'test'}:
        raise ValueError('Invalid frozen split assignments')
    result = frozen.split.to_numpy()
    if domain == 'uci':
        covariates = df[UCI_COLUMNS].to_numpy(dtype=float)
        if not np.all(np.isfinite(covariates)) or not np.array_equal(covariates, covariates.astype(np.int64)):
            raise ValueError('UCI canonical grouping requires exact integer-valued predictors')
        fingerprint = pd.util.hash_pandas_object(df[UCI_COLUMNS].astype(np.int64), index=False)
        if pd.DataFrame({'group': fingerprint, 'split': result}).groupby('group').split.nunique().gt(1).any():
            raise ValueError('Predictor-profile leakage across canonical splits')
        dev,test = next(GroupShuffleSplit(n_splits=1,test_size=.20,random_state=SEED).split(df,groups=fingerprint))
        train_local,val_local = next(GroupShuffleSplit(n_splits=1,test_size=.25,random_state=SEED+1).split(
            df.iloc[dev],groups=fingerprint.iloc[dev]))
        expected = np.full(len(df), '', dtype=object)
        expected[dev[train_local]] = 'train'
        expected[dev[val_local]] = 'validation'
        expected[test] = 'test'
        if not np.array_equal(result, expected):
            raise ValueError('UCI SHA-pinned split differs from integer-predictor grouped definition')
    else:
        unique_times = np.sort(df.Time.unique())
        a,b = unique_times[int(.60 * len(unique_times))], unique_times[int(.80 * len(unique_times))]
        expected = np.where(df.Time < a, 'train', np.where(df.Time < b, 'validation', 'test'))
        if not np.array_equal(result, expected):
            raise ValueError('ULB SHA-pinned split differs from time-ordered definition')
    return result


def model_object(name,domain,y):
    if name=='logistic':
        # No class weights: preserve event-rate interpretation in probabilities.
        model=LogisticRegression(C=1.0,solver='lbfgs',max_iter=2000,random_state=SEED)
    elif name=='xgboost':
        model=XGBClassifier(n_estimators=260,max_depth=3,learning_rate=.04,subsample=.85,
            colsample_bytree=.85,reg_lambda=8,min_child_weight=8,
            objective='binary:logistic',eval_metric='logloss',tree_method='hist',
            random_state=SEED,n_jobs=2)
    elif name=='lightgbm':
        model=LGBMClassifier(n_estimators=260,num_leaves=15,max_depth=5,learning_rate=.04,
            min_child_samples=70,colsample_bytree=.85,subsample=.85,subsample_freq=1,
            reg_lambda=8,verbosity=-1,random_state=SEED,n_jobs=2)
    else: raise ValueError('Unsupported model')
    # Scaling/imputation are each trained only with training subset.
    return Pipeline([('imputer',SimpleImputer(strategy='median')),
                     ('scaler',StandardScaler() if name=='logistic' else 'passthrough'),
                     ('estimator',model)])


def threshold_by_f1(y,p):
    from sklearn.metrics import precision_recall_curve
    precision,recall,thresholds=precision_recall_curve(y,p)
    f1=2*precision[:-1]*recall[:-1]/np.maximum(precision[:-1]+recall[:-1],1.e-12)
    return float(thresholds[int(np.nanargmax(f1))])


def metrics(y,p,threshold,domain,train_prevalence=None):
    y=np.asarray(y,dtype=int);p=np.asarray(p,dtype=float)
    if len(y)!=len(p) or not len(y):raise ValueError('Shape mismatch or empty predictions')
    if np.any(~np.isfinite(p)) or np.any((p<0)|(p>1)):raise ValueError('Invalid probabilities')
    pred=(p>=threshold).astype(int)
    tn,fp,fn,tp=map(int,confusion_matrix(y,pred,labels=[0,1]).ravel())
    out={'rows':len(y),'positive_count':int(y.sum()),'prevalence':float(np.mean(y)),
         'pr_auc_average_precision_sklearn':float(average_precision_score(y,p)),
         'roc_auc':float(roc_auc_score(y,p)) if len(np.unique(y))==2 else None,
         'log_loss':float(log_loss(y,p,labels=[0,1])),
         'brier':float(brier_score_loss(y,p)), 'mean_predicted_probability':float(p.mean()),
         'calibration_gap_observed_minus_expected':float(np.mean(y)-np.mean(p)),
         'threshold_selected_on_validation':float(threshold),
         'confusion_matrix':{'tn':tn,'fp':fp,'fn':fn,'tp':tp},
         'precision':float(precision_score(y,pred,zero_division=0)),
         'recall':float(recall_score(y,pred,zero_division=0)),
         'specificity':float(tn/(tn+fp)) if tn+fp else None,
         'f1':float(f1_score(y,pred,zero_division=0))}
    out['gini']=None if out['roc_auc'] is None else 2*out['roc_auc']-1
    if train_prevalence is not None:
        base=np.full(len(y),train_prevalence)
        out['baseline_train_prevalence']={'brier':float(brier_score_loss(y,base)),
            'log_loss':float(log_loss(y,base,labels=[0,1])),'average_precision':float(np.mean(y))}
    if domain=='ulb':
        budget=max(1,int(np.ceil(.001*len(y))))
        top=np.argsort(-p,kind='stable')[:budget]
        out['alert_budget']={'rate':.001,'count':int(budget),'precision_at_budget':float(y[top].mean()),
                             'fraud_recall_at_budget':float(y[top].sum()/max(1,y.sum()))}
        from sklearn.metrics import roc_curve
        fpr,tpr,_=roc_curve(y,p)
        feasible=tpr[fpr<=.001]
        out['recall_at_fpr_at_most_0_001']=float(feasible.max()) if len(feasible) else None
    return out


def fairness(df,y,p,threshold):
    """Descriptive UCI audits only; Wilson uncertainty; not a legal test."""
    from statsmodels.stats.proportion import proportion_confint
    audit=df[['SEX','AGE']].copy()
    audit['age_band']=pd.cut(audit.AGE,[-np.inf,29,44,59,np.inf],labels=['under_30','30_44','45_59','60_plus'])
    audit['prediction']=np.asarray(p)>=threshold
    audit['actual']=np.asarray(y)
    rows=[]
    for grouping in (['SEX'],['age_band'],['SEX','age_band']):
        for key,chunk in audit.groupby(grouping,observed=True,dropna=False):
            if not isinstance(key,tuple):key=(key,)
            n=len(chunk);pos=int(chunk.actual.sum());pred_pos=int(chunk.prediction.sum())
            tp=int((chunk.actual.astype(bool)&chunk.prediction).sum())
            fp=int((~chunk.actual.astype(bool)&chunk.prediction).sum())
            neg=n-pos
            def interval(k,d):return list(map(float,proportion_confint(k,d,method='wilson'))) if d else None
            rows.append({'group':dict(zip(grouping,map(str,key))),'n':n,'positives':pos,'negatives':neg,
                'selected':pred_pos,'selection_rate':pred_pos/n,'selection_rate_ci95_wilson':interval(pred_pos,n),
                'tpr':tp/pos if pos else None,'tpr_ci95_wilson':interval(tp,pos),
                'fpr':fp/neg if neg else None,'fpr_ci95_wilson':interval(fp,neg),
                'unstable':bool(n<100 or min(pos,neg)<30)})
    return rows


def save_json(path,obj):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path,'w',encoding='utf-8') as f:json.dump(obj,f,indent=2,allow_nan=False)


def calibration_bins(y,p,edges=None):
    """Frozen validation-risk quantile edges; no test-fitted bins."""
    y=np.asarray(y,dtype=int);p=np.asarray(p,dtype=float)
    if edges is None:
        edges=np.unique(np.quantile(p,np.linspace(0,1,11))).tolist()
        edges=[-float('inf')]+[float(x) for x in edges[1:-1]]+[float('inf')]
    labels=np.searchsorted(np.asarray(edges[1:-1]),p,side='right')
    records=[]
    for i in range(len(edges)-1):
        m=labels==i
        if m.any():
            records.append({'bin':i,'n':int(m.sum()),'positives':int(y[m].sum()),
                            'observed_rate':float(y[m].mean()),'predicted_mean':float(p[m].mean())})
    return records,edges

def bootstrap_intervals(y,p,seed=SEED,n_boot=250):
    """Simple iid percentile CIs: descriptive only, invalid under dependent records."""
    y=np.asarray(y,dtype=int);p=np.asarray(p,dtype=float)
    rng=np.random.default_rng(seed)
    ap=[];auc=[]
    for _ in range(n_boot):
        ix=rng.integers(0,len(y),len(y));z=y[ix]
        if len(np.unique(z))<2:continue
        ap.append(average_precision_score(z,p[ix]));auc.append(roc_auc_score(z,p[ix]))
    return {'method':'iid_percentile_bootstrap_95','n_resamples':n_boot,
        'note':'Descriptive only; independence/rare-positive limitations, not legal clearance',
        'pr_auc_ci95':list(map(float,np.quantile(ap,[.025,.975]))) if len(ap)>30 else None,
        'roc_auc_ci95':list(map(float,np.quantile(auc,[.025,.975]))) if len(auc)>30 else None}
