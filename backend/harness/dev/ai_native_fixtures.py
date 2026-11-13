"""Builds the fixtures for the live AI-native check. Runs inside the sandbox image, as trusted code.

    docker run --rm --network none \\
      -v LEGACY:/legacy:ro -v REPO:/repo:ro -v PARTITIONS:/partitions:ro -v OUT:/out IMAGE \\
      python /repo/backend/harness/dev/ai_native_fixtures.py

Everything official is computed the platform's way: predictions by each model's own scorer, the
validation reports by `app/services/validation_metrics.calculate` (Wit's pipeline under the frozen
policy), and PSI by `legacy_models/stability.py`. Seeded defects:

- archive_pii: a CRM export with synthetic personal data (names, emails, phone numbers).
- archive_broken: the Python model's saved file replaced by bytes that do not load.
- model_leaky: a model whose feature PAY_NEXT is a noisy copy of the target.
- triage_defect and investigation_defect: amounts in cents, a units change, not drift.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LEGACY, REPO, PARTITIONS, OUT = Path("/legacy"), Path("/repo"), Path("/partitions"), Path("/out")
TARGET = "default payment next month"
SEED = 20261108


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def score(script: Path, frame: pd.DataFrame, features: list[str], work: Path) -> pd.DataFrame:
    work.mkdir(parents=True, exist_ok=True)
    inputs, outputs = work / "inputs.csv", work / "scores.csv"
    frame[["row_id", *features]].to_csv(inputs, index=False)
    subprocess.run(
        [sys.executable, str(script), "--input", str(inputs), "--output", str(outputs)],
        check=True, cwd=script.parent,
    )
    return pd.read_csv(outputs, dtype={"row_id": str})


def report(name, artifact_sha, partitions, scores, work: Path) -> dict:
    sys.path[:0] = [str(REPO), str(LEGACY / "shared")]
    calculate = load("calculate", REPO / "backend/app/services/validation_metrics.py").calculate
    work.mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_text(json.dumps(
        {"dataset_id": "uci_credit", "name": name, "artifact_sha256": artifact_sha, "status": "uploaded"}))
    (work / "context.json").write_text(json.dumps(
        {"target_column": TARGET, "positive_class": 1, "lineage": {"fixture": "live_ai_native"},
         "checks": [], "stability": None}))
    (work / "saved_evaluation.json").write_text("{}")
    for part in ("validation", "test"):
        partitions[part].to_csv(work / f"{part}.csv", index=False)
        scores[part].to_csv(work / f"{part}_scores.csv", index=False)
    return calculate(work)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(SEED)
    parts = {p: pd.read_csv(PARTITIONS / f"{p}.csv", dtype={"row_id": str}) for p in ("train", "validation", "test")}
    manifest = json.loads((LEGACY / "credit_logistic_python/manifest.json").read_text())
    features = manifest["feature_order"]

    # Development data, one folder per dataset ID. Never validation or test rows.
    (OUT / "data/uci_credit").mkdir(parents=True, exist_ok=True)
    parts["train"].to_csv(OUT / "data/uci_credit/train.csv", index=False)

    # The clean model package and its official report.
    clean = OUT / "model_clean"
    for folder in ("credit_logistic_python", "shared"):
        shutil.copytree(LEGACY / folder, clean / folder, dirs_exist_ok=True)
    scorer = clean / "credit_logistic_python/score.py"
    scores = {p: score(scorer, parts[p], features, OUT / f"work/clean_{p}") for p in parts}
    clean_report = report("credit_logistic_python", sha256(clean / "credit_logistic_python/model.joblib"),
                          parts, scores, OUT / "work/report_clean")
    (OUT / "report_clean.json").write_text(json.dumps(clean_report, indent=2))
    (OUT / "document_clean").mkdir(exist_ok=True)
    shutil.copy(LEGACY / "credit_logistic_python/documentation/legacy_model_notes.md",
                OUT / "document_clean/model_document.md")

    # The leaky model: PAY_NEXT is the target with 3% of labels flipped.
    leaky_parts = {}
    for p, frame in parts.items():
        flip = rng.random(len(frame)) < 0.03
        leaky_parts[p] = frame.assign(PAY_NEXT=np.where(flip, 1 - frame[TARGET], frame[TARGET]).astype(int))
    leaky = OUT / "model_leaky/credit_payment_python"
    leaky.mkdir(parents=True, exist_ok=True)
    (leaky / "model.json").write_text(json.dumps(
        {"intercept": -2.4, "coefficients": {"PAY_0": 0.55, "PAY_NEXT": 4.1, "LIMIT_BAL": -2.0e-6}}))
    leaky_features = ["PAY_0", "PAY_NEXT", "LIMIT_BAL"]
    (leaky / "manifest.json").write_text(json.dumps(
        {"name": "credit_payment_python", "language": "Python", "feature_order": leaky_features,
         "target": TARGET, "positive_class": 1}))
    (leaky / "score.py").write_text(
        "import argparse, csv, json, math\n"
        "p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')\n"
        "a = p.parse_args()\n"
        "m = json.load(open('model.json'))\n"
        "with open(a.input) as f, open(a.output, 'w', newline='') as o:\n"
        "    w = csv.writer(o); w.writerow(['row_id', 'positive_probability'])\n"
        "    for r in csv.DictReader(f):\n"
        "        z = m['intercept'] + sum(c * float(r[k]) for k, c in m['coefficients'].items())\n"
        "        w.writerow([r['row_id'], repr(1 / (1 + math.exp(-z)))])\n"
    )
    (leaky / "train.py").write_text(
        "# Fitted in 2019 on the credit card portfolio.\n"
        "# PAY_NEXT: repayment status for the month after the snapshot, from the collections system.\n"
        "import pandas as pd\nfrom sklearn.linear_model import LogisticRegression\n"
        "df = pd.read_csv('train.csv')\n"
        "X = df[['PAY_0', 'PAY_NEXT', 'LIMIT_BAL']]\n"
        "LogisticRegression().fit(X, df['default payment next month'])\n"
    )
    leaky_scores = {p: score(leaky / "score.py", leaky_parts[p], leaky_features, OUT / f"work/leaky_{p}")
                    for p in ("validation", "test")}
    leaky_report = report("credit_payment_python", sha256(leaky / "model.json"),
                          leaky_parts, leaky_scores, OUT / "work/report_leaky")
    (OUT / "report_leaky.json").write_text(json.dumps(leaky_report, indent=2))
    (OUT / "data_leaky/uci_credit").mkdir(parents=True, exist_ok=True)
    leaky_parts["train"].to_csv(OUT / "data_leaky/uci_credit/train.csv", index=False)
    (OUT / "document_leaky").mkdir(exist_ok=True)
    (OUT / "document_leaky/model_document.md").write_text(
        "# credit_payment_python\n\n## Purpose\nPredicts default next month for credit card customers "
        "at the monthly snapshot, for limit management.\n\n## Features\nPAY_0 (repayment status in "
        "the snapshot month), PAY_NEXT (repayment status in the following month, from the "
        "collections system) and LIMIT_BAL.\n\n## Performance\nExcellent discrimination on holdout data."
        "\n\n## Limitations\nNone known.\n")

    # Monitoring: a frozen development baseline and four uploaded batches.
    stability = load("stability", LEGACY / "stability.py")
    policy = json.loads((LEGACY / "validation_policy.json").read_text())["stability"]
    ref_inputs, ref_scores = OUT / "work/ref_inputs.csv", OUT / "work/ref_scores.csv"
    parts["train"][["row_id", *features]].to_csv(ref_inputs, index=False)
    scores["train"].to_csv(ref_scores, index=False)
    baseline = stability.freeze_baseline(
        model_id="credit_logistic_python", feature_order=features, reference_inputs=ref_inputs,
        reference_scores=ref_scores, reference_scope="development", config=policy,
        artifact_sha256=sha256(clean / "credit_logistic_python/model.joblib"))
    val = parts["validation"]
    weights = np.where(val["LIMIT_BAL"] > val["LIMIT_BAL"].median(), 2.0, 1.0)
    small = val.sample(400, weights=weights, random_state=SEED)
    plain = val.sample(400, random_state=SEED + 2)
    cents = rng.random(len(plain)) < 0.2
    big = val.sample(2000, random_state=SEED + 1)
    shifted = big.copy()
    late = rng.random(len(shifted)) < 0.5
    shifted.loc[late, "PAY_0"] = np.clip(shifted.loc[late, "PAY_0"] + 2, -2, 8)
    windows = {
        "triage_clean": small,
        # One source system started sending 20% of the batch's bill amounts in cents.
        "triage_defect": plain.assign(**{
            f"BILL_AMT{i}": np.where(cents, plain[f"BILL_AMT{i}"] * 100, plain[f"BILL_AMT{i}"])
            for i in range(1, 7)
        }),
        "investigation_clean": shifted,
        "investigation_defect": big.assign(**{f"PAY_AMT{i}": big[f"PAY_AMT{i}"] * 100 for i in range(1, 7)}),
    }
    summaries = {}
    for name, frame in windows.items():
        folder = OUT / "monitoring" / name
        folder.mkdir(parents=True, exist_ok=True)
        batch = folder / "batch.csv"
        frame[["row_id", *features]].to_csv(batch, index=False)
        window_scores = score(scorer, frame, features, OUT / f"work/{name}")
        scores_path = OUT / f"work/{name}/window_scores.csv"
        window_scores.to_csv(scores_path, index=False)
        result = stability.evaluate_window(baseline=baseline, current_inputs=batch, current_scores=scores_path)
        result.update(input_scope="uploaded_batch", window_label=name.replace("_", " "))
        (folder / "monitoring_summary.json").write_text(json.dumps(result, indent=2, allow_nan=False))
        summaries[name] = {"band": result["band"], "score_band": result["score_band"],
                           "red_features": result["red_features"]}

    # Archives.
    def archive(name: str) -> Path:
        root = OUT / name
        for folder in ("credit_logistic_python", "credit_logistic_r"):
            shutil.copytree(LEGACY / folder, root / folder, dirs_exist_ok=True)
        (root / "shared").mkdir(exist_ok=True)
        for file in ("score_python.py", "pipeline.py"):
            shutil.copy(LEGACY / "shared" / file, root / "shared" / file)
        (root / "README.txt").write_text("Model files handed over by the credit risk team in 2021.\n")
        return root

    archive("archive_clean")
    pii = archive("archive_pii")
    (pii / "crm_export").mkdir()
    people = ["Alex Example", "Sam Placeholder", "Jordan Sample", "Riley Fixture"]
    rows = ["customer_name,email,phone,account_number,LIMIT_BAL"] + [
        f"{n},{n.split()[0].lower()}@example.com,+353 1 555 01{i:02d},IE00TEST{i:08d},{50000 + i * 1000}"
        for i, n in enumerate(people * 5)
    ]
    (pii / "crm_export/customer_contacts.csv").write_text("\n".join(rows) + "\n")
    broken = archive("archive_broken")
    (broken / "credit_logistic_python/model.joblib").write_bytes(b"this is not a joblib file\n" * 20)

    (OUT / "fixtures.json").write_text(json.dumps({
        "features": features,
        "clean_auc": {p: clean_report["saved_metrics"]["fresh_evaluation"][p]["roc_auc"] for p in ("validation", "test")},
        "leaky_auc": {p: leaky_report["saved_metrics"]["fresh_evaluation"][p]["roc_auc"] for p in ("validation", "test")},
        "monitoring": summaries,
    }, indent=2))
    shutil.rmtree(OUT / "work")


if __name__ == "__main__":
    main()
