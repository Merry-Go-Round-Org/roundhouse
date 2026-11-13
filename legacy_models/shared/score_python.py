"""Strict CSV scorer; explicitly refuses unsafe inputs and unloaded models."""
import argparse
import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import joblib
from pipeline import sha256


def score(package,input_csv,output_csv):
    package=Path(package)
    manifest=json.loads((package/'manifest.json').read_text())
    if manifest['language']!='Python':raise ValueError('Wrong-language model manifest')
    artifact=package/'model.joblib'
    if not artifact.is_file():raise FileNotFoundError(f'Trained artifact absent: {artifact}')
    if sha256(artifact)!=manifest.get('artifact_sha256'):raise ValueError('Saved model artifact SHA-256 does not match manifest')
    data=pd.read_csv(input_csv,dtype={'row_id':str},encoding='utf-8')
    if not len(data):raise ValueError('No input rows')
    if data.columns.duplicated().any():raise ValueError('Duplicate input columns')
    if 'row_id' not in data or data.row_id.isna().any() or data.row_id.duplicated().any() or (data.row_id.str.strip()=='').any():
        raise ValueError('row_id must be unique, non-empty and non-null')
    features=manifest['feature_order']
    missing=[f for f in features if f not in data.columns]
    if missing:raise ValueError(f'Missing required columns: {missing}')
    if any(f in data.columns for f in ('Class','default payment next month')):
        raise ValueError('Outcome labels forbidden in scoring request')
    X=data[features].apply(pd.to_numeric,errors='raise')
    if not np.isfinite(X.to_numpy(dtype=float)).all():raise ValueError('Invalid or missing numeric features')
    # joblib deserialisation can execute code. Load only trusted, checksum-verified packages.
    model=joblib.load(artifact)
    p=model.predict_proba(X)[:,1]
    if not np.isfinite(p).all() or (p<0).any() or (p>1).any():raise ValueError('Invalid probabilities')
    pd.DataFrame({'row_id':data.row_id,'positive_probability':p}).to_csv(output_csv,index=False,float_format='%.17g')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--package',default=str(Path(__file__).resolve().parent))
    args=parser.parse_args()
    try:score(args.package,args.input,args.output)
    except Exception as error:
        print(f'SCORING FAILED: {type(error).__name__}: {error}',file=sys.stderr)
        sys.exit(2)
