# Rebuild-only environment for the EXL eight binary legacy models.
# Uses the same Python versions as the existing verified Linux provenance job.
# R is supplied by Debian bookworm apt; its package versions are recorded at
# runtime, not asserted bit-identical to historical macOS R builds.
FROM --platform=linux/amd64 python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git r-base-core r-cran-readxl r-cran-jsonlite r-cran-digest libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY legacy_models/provenance/requirements.txt /tmp/repro-requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    -r /tmp/repro-requirements.txt

WORKDIR /workspace
ENTRYPOINT ["python", "legacy_models/reproduction/reproduce.py"]
