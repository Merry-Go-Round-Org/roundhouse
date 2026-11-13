"""Real native-bundle scoring; optional Docker, no provider calls or credentials."""

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.config import settings
from app.services import native_scoring

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_DOCKER_TESTS") != "1",
    reason="Explicit local Docker opt-in required",
)


def test_nested_original_bundle_and_reviewed_wrapper_reproduce(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    stage = tmp_path / "stage"
    for folder in (
        stage / "package/original",
        stage / "package/scoring_wrapper",
        stage / "data",
    ):
        folder.mkdir(parents=True, exist_ok=True)
    (stage / "package/original/weights.json").write_text('{"low":0.2,"high":0.8}')
    (
        stage / "package/scoring_wrapper/score.py"
    ).write_text("""import argparse,csv,json,pathlib
p=argparse.ArgumentParser()
p.add_argument('--input');p.add_argument('--output');p.add_argument('--model-dir')
a=p.parse_args(); weights=json.loads((pathlib.Path(a.model_dir)/'weights.json').read_text())
with open(a.input) as source,open(a.output,'w') as output:
 writer=csv.writer(output);writer.writerow(['row_id','positive_probability'])
 for row in csv.DictReader(source):
  writer.writerow([row['row_id'],weights['high' if float(row['value'])>0 else 'low']])
""")
    (stage / "package/manifest.json").write_text(
        json.dumps(
            {
                "ai_native": True,
                "executable": True,
                "entry_point": "scoring_wrapper/score.py",
                "pass_model_dir": True,
                "model_subdir": "original",
                "feature_order": ["value"],
            }
        )
    )
    (stage / "data/development_inputs.csv").write_text("row_id,value\na,-1\nb,1\n")
    hashes = {
        str(file.relative_to(stage)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in stage.rglob("*")
        if file.is_file()
    }
    sha = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    snapshot = tmp_path / "packages" / sha
    snapshot.parent.mkdir()
    stage.rename(snapshot)
    package = SimpleNamespace(id=str(uuid4()), sha256=sha, file_hashes=hashes, n_rows=2)
    reference = tmp_path / "reference.csv"
    reference.write_text("row_id,positive_probability\na,0.2\nb,0.8\n")
    result = asyncio.run(native_scoring.reproduce(package, reference))
    assert result["status"] == "PASS", result
    assert result["comparison"]["max_abs_diff"] == 0
    reference.write_text("row_id,positive_probability\na,0.3\nb,0.8\n")
    result = asyncio.run(native_scoring.reproduce(package, reference))
    assert result["status"] == "FAIL", result
