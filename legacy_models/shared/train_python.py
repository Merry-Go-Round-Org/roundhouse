"""Train one frozen-split Python legacy package. No network or test-time tuning."""
import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
import joblib
import numpy as np
import pandas as pd

from pipeline import (DATA,SEED,read_data,freeze_split,model_object,
                      threshold_by_f1,metrics,fairness,sha256,save_json,calibration_bins,bootstrap_intervals)


def train(domain,algorithm,dataset,root):
    package=Path(root).resolve()
    base=package.parent
    evidence=base/'evidence'
    df,checksum=read_data(domain,dataset)
    splits=freeze_split(domain,df,evidence)
    feature_names=DATA[domain]['features'];target=DATA[domain]['target']
    X=df[feature_names]
    y=df[target].astype(int).to_numpy()
    train_mask=splits=='train';val_mask=splits=='validation';test_mask=splits=='test'
    model=model_object(algorithm,domain,y[train_mask])
    model.fit(X.loc[train_mask],y[train_mask])
    val_p=model.predict_proba(X.loc[val_mask])[:,1]
    threshold=threshold_by_f1(y[val_mask],val_p)
    package.mkdir(parents=True,exist_ok=True)
    artifact=package/'model.joblib';joblib.dump(model,artifact)
    loaded=joblib.load(artifact)
    check=loaded.predict_proba(X.loc[val_mask])[:,1]
    if not np.allclose(val_p,check,rtol=0,atol=1e-12):raise RuntimeError('Artifact reload prediction mismatch')
    train_rate=float(y[train_mask].mean())
    summary={'model_version':'1.0.0','domain':domain,'algorithm':algorithm,'dataset_sha256':checksum,
        'seed':SEED,'feature_names':feature_names,'split_file':'legacy_models/evidence/'+domain+'_split_ids.csv',
        'split_sha256':sha256(evidence/f'{domain}_split_ids.csv'),
        'preprocessing':'training-median imputation and logistic-only standardisation',
        'positive_class':1,'validation':metrics(y[val_mask],val_p,threshold,domain,train_rate),
        'test':None,'artifact_sha256':sha256(artifact),'evidence_status':'executed_full_data'}
    # Test untouched until all algorithms and thresholds are frozen. Report test only after
    # validation config/artifact are committed by training command, never tune on it.
    test_p=loaded.predict_proba(X.loc[test_mask])[:,1]
    summary['test']=metrics(y[test_mask],test_p,threshold,domain,train_rate)
    summary['validation']['bootstrap_uncertainty']=bootstrap_intervals(y[val_mask],val_p)
    summary['test']['bootstrap_uncertainty']=bootstrap_intervals(y[test_mask],test_p)
    bins,edges=calibration_bins(y[val_mask],val_p)
    summary['validation']['calibration_bins']=bins
    summary['test']['calibration_bins']=calibration_bins(y[test_mask],test_p,edges)[0]
    summary['calibration_bin_edges']=[str(x) for x in edges]
    if domain=='uci':
        summary['fairness_test_descriptive']=fairness(df.loc[test_mask],y[test_mask],test_p,threshold)
    else:
        summary['fairness_test_descriptive']='not_assessable_no_protected_demographics'
    dev_rows=df.loc[val_mask,['row_id']+feature_names].head(100)
    (package/'fixtures').mkdir(exist_ok=True)
    dev_rows.to_csv(package/'fixtures/development_inputs.csv',index=False)
    pd.DataFrame({'row_id':dev_rows.row_id,
                  'positive_probability':loaded.predict_proba(pd.read_csv(package/'fixtures/development_inputs.csv')[feature_names])[:,1]}).to_csv(
                    package/'fixtures/reference_predictions.csv',index=False)
    # Test records/labels never shipped in the development fixtures.
    # Protected test labels are local-only; never hand this directory to the migration agent.
    private=evidence/'private_test';private.mkdir(parents=True,exist_ok=True)
    pd.DataFrame({'row_id':df.loc[test_mask,'row_id'],'positive_probability':test_p,
                  'label':y[test_mask]}).to_csv(private/f'{package.name}_test_predictions.csv',index=False)
    summary['private_test_predictions']='legacy_models/evidence/private_test/'+package.name+'_test_predictions.csv'
    save_json(package/'evaluation.json',summary)
    save_json(package/'manifest.json',{
        'contract_version':'1.0.0','model_version':'1.0.0','name':package.name,
        'language':'Python','domain':'credit' if domain=='uci' else 'fraud',
        'dataset_id':'uci_credit' if domain=='uci' else 'ulb_fraud',
        'task':'binary_classification','algorithm':algorithm,'entry_point':'score.py',
        'artifact_paths':['model.joblib'],'artifact_sha256':sha256(artifact),
        'dependency_reference':'requirements.txt','input_schema':'schema.json',
        'output_schema':'schema.json#/output','dataset_sha256':checksum,
        'target':target,'positive_class':1,'seed':SEED,'feature_order':feature_names,
        'split_definition':summary['split_file'],'frozen_split_sha256':sha256(evidence/f'{domain}_split_ids.csv'),
        'preprocessing':summary['preprocessing'],
        'metrics_reference':'evaluation.json','fixtures':'fixtures/',
        'limitations':['Research only, not a regulatory PD or certified banking model',
                       'No real dated cohorts' if domain=='uci' else 'Two-day relative transaction time, opaque PCA, no demographic fairness']})
    print(json.dumps({'package':package.name,'validation_ap':summary['validation']['pr_auc_average_precision_sklearn'],
        'test_ap':summary['test']['pr_auc_average_precision_sklearn'],'artifact_sha256':sha256(artifact)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--domain',choices=['uci','ulb'],required=True)
    parser.add_argument('--algorithm',choices=['logistic','xgboost','lightgbm'],required=True)
    parser.add_argument('--data',required=True)
    parser.add_argument('--package',required=True)
    args=parser.parse_args()
    try:train(args.domain,args.algorithm,args.data,args.package)
    except Exception as error:
        print(f'TRAINING FAILED: {type(error).__name__}: {error}',file=sys.stderr)
        sys.exit(1)
