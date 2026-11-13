# The agent sandbox: everything rebuild, parity and document tasks need, because it has no network.
# Build from backend/, for both architectures:
#   docker buildx build --platform linux/amd64,linux/arm64 -t merry-go-round/agent-sandbox:0.2 --load harness/sandbox_image
# Pull a published image by its digest, never by tag. Datasets and models are never in the image:
# the backend chooses them for each run and they are mounted read-only.
# Pinned by digest. Debian trixie is the first with r-cran-renv, so every R package comes from apt.
FROM python:3.12-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

LABEL org.opencontainers.image.title="merry-go-round agent sandbox" \
      org.opencontainers.image.version="0.2" \
      org.opencontainers.image.source="https://github.com/Merry-Go-Round-Org/merry_go_round_workspace"

# apt is pinned to one Debian snapshot, so a later build installs the same versions.
# Snapshots are old by design, which is why the Valid-Until check is off. Packages stay signed.
# snapshot.debian.org answers 503 under load and apt only ignores a missing index, hence the loop.
# libgomp1: OpenMP, for lightgbm and xgboost.
# r-cran-*: what the R models load. renv records a verified snapshot, per REPRODUCTION.md.
# findutils, coreutils and diffutils: the sandbox tools use find, realpath, tee, head and timeout.
RUN sed -i \
        -e 's|^URIs: http://deb.debian.org/debian$|URIs: http://snapshot.debian.org/archive/debian/20261104T000000Z|' \
        -e 's|^URIs: http://deb.debian.org/debian-security$|URIs: http://snapshot.debian.org/archive/debian-security/20261104T000000Z|' \
        /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Acquire::Check-Valid-Until "false";' \
        'Acquire::Retries "10";' \
        'Acquire::Queue-Mode "access";' \
        'Acquire::http::Pipeline-Depth "0";' \
        > /etc/apt/apt.conf.d/99snapshot \
    && for attempt in 1 2 3 4 5 6 7 8; do \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            coreutils diffutils findutils libgomp1 \
            r-base-core r-cran-digest r-cran-jsonlite r-cran-readxl r-cran-renv \
        && break; \
        [ "$attempt" -lt 8 ] || exit 1; \
        sleep $((attempt * 30)); \
    done \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt lists what we chose. requirements.lock pins everything, with hashes.
COPY requirements.txt requirements.lock verify_image.py /opt/sandbox/
RUN pip install --no-cache-dir --no-compile --require-hashes --only-binary :all: -r /opt/sandbox/requirements.lock

# A new named volume takes its ownership from this directory, so /workspace must belong to
# the sandbox user. The root filesystem is read-only at run time.
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /tmp agent \
    && mkdir -p /workspace /inputs/model /inputs/data \
    && chown agent:agent /workspace

# Thread counts match legacy_models/provenance/Dockerfile. Left unset, OpenMP follows the host's
# core count, so results and timing could change from machine to machine.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PIP_NO_INPUT=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=2 PYTHONHASHSEED=0
USER 1000:1000
WORKDIR /workspace

# Fail the build rather than ship an image that is missing something.
RUN python /opt/sandbox/verify_image.py
