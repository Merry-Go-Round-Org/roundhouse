"""Freeze exactly the same row IDs/splits for R and Python; no downloading."""
import argparse,json,sys
from pathlib import Path
from engine import dataset,split,PATH,TARGET,SEED

def main():
 p=argparse.ArgumentParser()
 p.add_argument('--domain',required=True,choices=['uci','ulb'])
 p.add_argument('--data')
 a=p.parse_args()
 data,sha=dataset(a.domain,a.data or PATH[a.domain])
 folder=Path(__file__).resolve().parents[1]/'evidence'
 parts=split(a.domain,data,folder)
 summary={'domain':a.domain,'dataset_sha256':sha,'seed':SEED,
    'status':'executed_full_data','split_method':'grouped_predictor_profile' if a.domain=='uci' else 'relative_time_ordered_ties_together',
    'partitions':{name:{'rows':int((parts==name).sum()),
       'positive_count':int(data.loc[parts==name,TARGET[a.domain]].sum())}
       for name in ['train','validation','test']}}
 (folder/(a.domain+'_split_summary.json')).write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps(summary,indent=2))
if __name__=='__main__':
 try:main()
 except Exception as exc:print('SPLIT FAILED:',repr(exc),file=sys.stderr);sys.exit(1)
