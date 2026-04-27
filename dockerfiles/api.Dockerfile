# Python 3.14.4 base — matches pyproject.toml's requires-python exactly.
# Slim variant (~50MB) keeps the final image lean; Chromium and its
# system deps come from Playwright's own installer below.
#
# We don't use mcr.microsoft.com/playwright/python because that image
# pins to Ubuntu Noble's Python 3.12 and we'd need a second Python
# alongside. Single-Python install reads more cleanly and gives a
# smaller final image (~600MB vs ~1.5GB).
FROM python:3.14.4-slim

WORKDIR /app

# uv: matches the project's build backend (uv_build) and is much faster
# than pip for cold-cache resolves.
RUN pip install --no-cache-dir uv

# Project metadata + source. Editable install needs the layout, so we
# copy together rather than splitting "deps first / source later" — the
# layer-cache win there is small for this size of project.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install our package + the [server] extra into the system Python.
# (No venv: single-purpose container, system Python is the venv.)
RUN uv pip install --system --no-cache -e ".[server]"

# Download Chromium and install its system dependencies via apt.
# Playwright's installer reads the playwright==1.58.0 we just installed
# and pulls the matching browser build into /root/.cache/ms-playwright.
RUN playwright install --with-deps chromium

# Job artifacts live here — mounted as a named volume in compose so they
# survive container restarts.
ENV RANKER_SERVICE_JOBS_DIR=/data/jobs \
    RANKER_SERVICE_MAX_CONCURRENT_JOBS=4 \
    RANKER_BIN=/usr/local/bin/ranker
RUN mkdir -p /data/jobs

EXPOSE 8000

CMD ["uvicorn", "ranker_service.api:app", \
     "--host", "0.0.0.0", "--port", "8000"]
