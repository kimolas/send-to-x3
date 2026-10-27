# Send to X3: Home Assistant app image. It also runs with plain `docker run`.
# https://contributors.home-assistant.io/apps/guides/dockerfile/
#
# Since Supervisor 2026.04 BUILD_FROM is no longer passed, so this default is the
# base image. The Supervisor and the builder actions pass BUILD_ARCH and
# BUILD_VERSION and add the io.hass.arch / io.hass.version labels themselves.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

ARG BUILD_VERSION=dev

# tesseract + English data for the optional OCR of scanned PDFs (option pdf_ocr; PDF
# text extraction itself uses pypdfium2). bash, curl, jq, tzdata and CA certificates
# are already in the base image.
RUN apk add --no-cache tesseract-ocr tesseract-ocr-data-eng

# Python dependencies. Wheels only, so a dependency without a musllinux wheel fails
# the build at once instead of compiling for an hour on a Raspberry Pi. --isolated
# ignores the base image's pip config and PIP_EXTRA_INDEX_URL, so every package
# comes from PyPI at exactly the pinned version.
COPY requirements.txt /tmp/requirements.txt
RUN pip install \
        --isolated \
        --no-cache-dir \
        --only-binary=:all: \
        --disable-pip-version-check \
        --root-user-action=ignore \
        -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# The application package. Stale bytecode from the build context is dropped and
# everything is compiled once here, so nothing writes .pyc files at runtime.
COPY sendtox3 /opt/sendtox3/sendtox3
RUN find /opt/sendtox3 -type d -name __pycache__ -prune -exec rm -rf {} + \
    && python3 -m compileall -q /opt/sendtox3

COPY run.sh /run.sh
RUN chmod 0755 /run.sh

ENV \
    PYTHONPATH=/opt/sendtox3 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_THREAD_LIMIT=1

WORKDIR /opt/sendtox3

LABEL \
    io.hass.type="app" \
    io.hass.name="Send to X3" \
    io.hass.description="Share web pages, notes, emails and PDFs to an Xteink X3 e-reader as clean, image-free text" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Send to X3" \
    org.opencontainers.image.description="Share web pages, notes, emails and PDFs to an Xteink X3 e-reader as clean, image-free text" \
    org.opencontainers.image.version="${BUILD_VERSION}"

# s6-overlay's /init (the base image ENTRYPOINT) runs this; never set ENTRYPOINT.
CMD ["/run.sh"]
