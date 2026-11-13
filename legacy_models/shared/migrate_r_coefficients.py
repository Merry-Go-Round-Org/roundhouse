"""Replay the exact fitted R GLM coefficients on identical feature rows (never refit)."""
import argparse,json,sys
from pathlib import Path
import numpy as np,pandas as pd
from scipy.special import expit
def main(package,source,target):
 coef=json.loads((Path(package)/'r_coefficients.json').read_text())
 f=coef['feature_order'];df=pd.read_csv(source,dtype={'row_id':str})
 if not len(df) or 'row_id' not in df or df.row_id.isna().any() or df.row_id.duplicated().any() or (df.row_id.str.strip()=='').any():
  raise ValueError('Invalid/missing row_id')
 if not set(f).issubset(df.columns):raise ValueError('Missing R model feature')
 X=df[f].apply(pd.to_numeric,errors='raise').to_numpy(dtype=float)
 if not np.isfinite(X).all():raise ValueError('Nonfinite input')
 b=np.asarray(coef['coefficients'],dtype=float)
 if len(b)!=len(f):raise ValueError('Coefficient count mismatch')
 pred=expit(float(coef['intercept'])+X@b)
 if not np.isfinite(pred).all():raise ValueError('Invalid scores')
 pd.DataFrame({'row_id':df.row_id,'positive_probability':pred}).to_csv(target,index=False,float_format='%.17g')
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--package',required=True)
 a.add_argument('--input',required=True);a.add_argument('--output',required=True)
 args=a.parse_args()
 try:main(args.package,args.input,args.output)
 except Exception as e:print('R MIGRATION SCORING ERROR:',repr(e),file=sys.stderr);sys.exit(2)
