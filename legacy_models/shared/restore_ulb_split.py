"""Restore the immutable ULB time-ordered row-ID split without raw transactions.

The split is contiguous by transaction row index and fixed at 162992/224667.
Only writes if absent. Aborts rather than overwriting a modified split.
"""
from pathlib import Path
import hashlib

CANONICAL_SHA256 = '5d9edf4df20e866d3198e74893fd2e1cf75f5fc3c5b4e175efd5a4a478b827fa'
N_ROWS = 284807
TRAIN_END = 162992
VALIDATION_END = 224667

def restore(path):
    path = Path(path)
    if path.exists():
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        if h != CANONICAL_SHA256:
            raise ValueError(f'Existing ULB frozen split checksum mismatch: {h}')
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.pending')
    try:
        with pending.open('w', encoding='utf-8', newline='') as stream:
            stream.write('row_id,split\n')
            for idx in range(N_ROWS):
                split = 'train' if idx < TRAIN_END else 'validation' if idx < VALIDATION_END else 'test'
                stream.write(f'ulb-{idx:06d},{split}\n')
        actual = hashlib.sha256(pending.read_bytes()).hexdigest()
        if actual != CANONICAL_SHA256:
            raise ValueError(f'Restored ULB split checksum mismatch: {actual}')
        pending.replace(path)
        return path
    finally:
        if pending.exists():
            pending.unlink()

if __name__ == '__main__':
    filename = Path(__file__).resolve().parents[1] / 'evidence/ulb_split_ids.csv'
    print(restore(filename))
