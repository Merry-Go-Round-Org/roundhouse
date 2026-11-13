#!/usr/bin/env python3
import sys
from pathlib import Path
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shared'))
from train_python import train
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data',default=str(Path(__file__).resolve().parents[2]/'datasets'/'ulb'/'creditcard.csv'))
    args=parser.parse_args()
    try:train('ulb','xgboost',args.data,Path(__file__).resolve().parent)
    except Exception as error:print(f'TRAINING ERROR: {error}',file=sys.stderr);sys.exit(1)
