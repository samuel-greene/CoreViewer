# For more information, please refer to https://aka.ms/vscode-docker-python
FROM python:3-slim

EXPOSE 8000

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

# Install native libvips required by pyvips
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libvips42 \
    && rm -rf /var/lib/apt/lists/*

# Install pip requirements
COPY requirements.txt .
RUN python -m pip install -r requirements.txt

WORKDIR /app
COPY . /app

# Persist the image creation time for the login screen. CI may provide an
# explicit UTC value with --build-arg BUILD_TIMESTAMP=... for reproducibility.
ARG BUILD_TIMESTAMP
RUN build_timestamp="${BUILD_TIMESTAMP:-$(date -u +'%Y-%m-%dT%H:%M:%SZ')}" \
    && printf '{"created_at":"%s","source":"container"}\n' "$build_timestamp" > /app/.build-info.json

# Run the codec and authentication regression tests in the shipped runtime.
RUN python -m unittest discover -s tests

# Creates a non-root user with an explicit UID and adds permission to access the /app folder
# For more info, please refer to https://aka.ms/vscode-docker-python-configure-containers
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

# During debugging, this entry point will be overridden. For more information, please refer to https://aka.ms/vscode-docker-python-debug
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
