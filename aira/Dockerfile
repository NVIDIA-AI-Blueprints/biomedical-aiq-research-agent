FROM nvcr.io/nvidia/base/ubuntu:jammy-20250415.1


# Install uv package manager
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip uv

# Ensure the installed binary is on the `PATH`
ENV PATH="/root/.local/bin/:$PATH"

RUN uv python install 3.12

# Copy the local build context (your project files) into the container's /app directory
COPY ./aira/entrypoint.sh /entrypoint.sh
COPY ./aira/. /app


RUN uv venv --python-preference managed
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AIQ_AIRA="0.0.0"

# Use uv to install Python dependencies 
RUN uv pip install /app 

RUN chmod +x /entrypoint.sh
