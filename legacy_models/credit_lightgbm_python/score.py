#!/usr/bin/env python3
import sys
from pathlib import Path
import argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'shared'))
from score_python import score
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:score(Path(__file__).resolve().parent,args.input,args.output)
    except Exception as error:print(f'SCORING ERROR: {error}',file=sys.stderr);sys.exit(2)
