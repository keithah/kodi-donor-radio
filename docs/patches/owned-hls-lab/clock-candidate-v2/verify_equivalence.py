#!/usr/bin/env python3
"""Compare complete v1/v2 output dictionaries for archived finite ledgers."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--v1',type=Path,required=True)
    ap.add_argument('--ledger',type=Path,action='append',required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    v1=load('frozen_v1',args.v1)
    v2=load('candidate_v2',Path(__file__).with_name('estimator.py'))
    records=[]
    for path in args.ledger:
        one,two=v1.Estimator(),v2.Estimator()
        count=0; digest=hashlib.sha256()
        for line in path.open():
            row=json.loads(line)
            if row['kind']!='clock_sample':
                continue
            if not v2.finite_number(row.get('audioengine_delay_s')):
                raise ValueError('Finite-input equivalence requires finite delay: '+str(path))
            old,new=one.update(row),two.update(row)
            if old!=new:
                raise AssertionError('Changed finite-input output at row %d in %s'%(count,path))
            encoded=json.dumps(new,sort_keys=True,allow_nan=False)
            digest.update((encoded+'\n').encode())
            count+=1
        records.append(dict(ledger=str(path),ledger_sha256=v2.sha(path),
                            clock_rows=count,identical=True,timeline_sha256=digest.hexdigest()))
    result=dict(v1_sha256=v2.sha(args.v1),v2_sha256=v2.sha(Path(__file__).with_name('estimator.py')),
                comparison='Exact output dictionary equality at every native clock row, no waveform input',ledgers=records)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
