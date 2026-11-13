"""Download and extract the project's public datasets into datasets/.

Usage: python download_datasets.py [uci] [ulb] [--force]
See DATASETS.md for details.
"""
import argparse
import hashlib
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parent / 'datasets'
CHUNK_SIZE = 1024 * 1024
DATASETS = {
    'uci': {
        'url': 'https://archive.ics.uci.edu/static/public/350/default+of+credit+card+clients.zip',
        'file': 'default of credit card clients.xls',
        'sha256': '30c6be3abd8dcfd3e6096c828bad8c2f011238620f5369220bd60cfc82700933',
    },
    'ulb': {
        'url': 'https://www.kaggle.com/api/v1/datasets/download/mlg-ulb/creditcardfraud',
        'file': 'creditcard.csv',
        'sha256': '76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89',
    },
}


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'merry-go-round-datasets'})
    with urllib.request.urlopen(request, timeout=60) as response, path.open('wb') as f:
        shutil.copyfileobj(response, f, CHUNK_SIZE)


def extract(zip_path, filename, path):
    """Write only the expected member, so the archive cannot write elsewhere."""
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.namelist() if Path(m).name == filename]
        if len(members) != 1:
            raise ValueError(f'expected one {filename!r} in the archive, found {len(members)}')
        with archive.open(members[0]) as source, path.open('wb') as target:
            shutil.copyfileobj(source, target, CHUNK_SIZE)


def fetch(name, force=False):
    spec = DATASETS[name]
    target_dir = DATASETS_DIR / name
    target = target_dir / spec['file']
    if not force and target.exists() and sha256_of(target) == spec['sha256']:
        print(f'{name}: already present at {target}')
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir / f'{name}.zip.part'
    extracted = target_dir / f"{spec['file']}.part"
    try:
        print(f"{name}: downloading {spec['url']}")
        download(spec['url'], zip_path)
        print(f'{name}: extracting')
        extract(zip_path, spec['file'], extracted)
        actual = sha256_of(extracted)
        if actual != spec['sha256']:
            raise ValueError(f"SHA-256 mismatch: expected {spec['sha256']}, got {actual}")
        extracted.replace(target)
        print(f'{name}: saved to {target}')
    finally:
        zip_path.unlink(missing_ok=True)
        extracted.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('datasets', nargs='*', metavar='dataset', help='uci and/or ulb; defaults to both')
    parser.add_argument('--force', action='store_true', help='download again even if a valid copy exists')
    args = parser.parse_args()
    unknown = sorted(set(args.datasets) - set(DATASETS))
    if unknown:
        parser.error(f"unknown dataset: {', '.join(unknown)}")

    failed = []
    for name in args.datasets or DATASETS:
        try:
            fetch(name, args.force)
        except (OSError, urllib.error.URLError, ValueError, zipfile.BadZipFile) as error:
            print(f'{name}: FAILED: {error}', file=sys.stderr)
            failed.append(name)
    if failed:
        sys.exit(f"Failed: {', '.join(failed)}. See DATASETS.md for manual download steps.")


if __name__ == '__main__':
    main()
