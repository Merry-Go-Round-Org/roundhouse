"""Frozen v1.0.0 R/Python coefficient parity policy: atol=1e-8, rtol=0."""
import argparse,json,sys
import numpy as np,pandas as pd
def main(r_path,python_path,output):
 a=pd.read_csv(r_path,dtype={'row_id':str});b=pd.read_csv(python_path,dtype={'row_id':str})
 if not len(a) or not a.row_id.equals(b.row_id) or a.row_id.isna().any() or a.row_id.duplicated().any():
  raise ValueError('row_id order/alignment mismatch')
 p=a.positive_probability.to_numpy();q=b.positive_probability.to_numpy()
 if not np.isfinite(p).all() or not np.isfinite(q).all() or ((p<0)|(p>1)).any() or ((q<0)|(q>1)).any():
  raise ValueError('Invalid probabilities')
 d=np.abs(p-q)
 result={'parity_contract_version':'1.0.0','tolerance_absolute':1e-8,'tolerance_relative':0,
  'row_alignment':'exact_order','n_rows':len(p),'max_absolute_difference':float(d.max()),
  'mean_absolute_difference':float(d.mean()),'class_agreement_at_0_5':float(np.mean((p>=.5)==(q>=.5))),
  'preprocessing':'R train-only centred/scaled coefficients converted back to original units for Python scoring',
  'passed':bool(np.all(d<=1e-8))}
 with open(output,'w') as f:json.dump(result,f,indent=2)
 print(json.dumps(result,indent=2))
 if not result['passed']:sys.exit(1)
if __name__=='__main__':
 arg=argparse.ArgumentParser();arg.add_argument('--r',required=True);arg.add_argument('--python',required=True);arg.add_argument('--output',required=True)
 a=arg.parse_args()
 try:main(a.r,a.python,a.output)
 except Exception as e:print('PARITY INVALID:',repr(e),file=sys.stderr);sys.exit(2)
