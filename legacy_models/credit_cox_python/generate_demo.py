"""Generate SYNTHETIC right-censored loan durations; NOT UCI/ULB observations."""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd

def generate(n=2500, seed=20261104):
    if n < 100:
        raise ValueError("n must be >= 100")
    rng = np.random.default_rng(seed)
    utilisation = rng.beta(2.5, 3.5, n)
    income_to_debt = rng.lognormal(-0.4, 0.6, n)
    delinquencies = np.minimum(rng.poisson(0.7, n), 8).astype(float)
    log_risk = 1.4 * utilisation + 0.40 * income_to_debt + 0.35 * delinquencies
    event_time = rng.exponential(1.0, n) / (0.013 * np.exp(log_risk))
    censor_time = rng.uniform(8, 42, n)
    duration = np.minimum(event_time, censor_time)
    event = (event_time <= censor_time).astype(int)
    return pd.DataFrame({
        "row_id": [f"syncox-{i:06d}" for i in range(n)],
        "duration": duration,
        "event": event,
        "utilisation": utilisation,
        "income_to_debt": income_to_debt,
        "delinquencies": delinquencies,
    })

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="datasets/synthetic/cox_loans.csv")
    ap.add_argument("--n", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=20261104)
    a = ap.parse_args()
    output = Path(a.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    df = generate(a.n, a.seed)
    df.to_csv(output, index=False, float_format="%.17g")
    print(f"SYNTHETIC ONLY: {len(df)} rows, {int(df.event.sum())} events -> {output}")

if __name__ == "__main__":
    main()
