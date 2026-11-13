# Deliberately run on Linux/amd64 to match the platform where the
# saved XGBoost release was independently reproduced.
FROM python:3.13.5-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=2 \
    PYTHONHASHSEED=0
# The slim Python base omits the GNU OpenMP runtime required by LightGBM.
# Install only the shared runtime; the compiler toolchain is not needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/provenance-requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /tmp/provenance-requirements.txt
WORKDIR /workspace
ENTRYPOINT ["python", "legacy_models/shared/verify_split_provenance.py"]
