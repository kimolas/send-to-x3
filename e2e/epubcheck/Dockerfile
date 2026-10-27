# epubcheck 5.4.0 (W3C, BSD-3-Clause) on a small JRE, for validating generated EPUBs.
#   docker build -t sendtox3-epubcheck:5.4.0 e2e/epubcheck
#   docker run --rm --network none -v "$PWD/out:/data:ro" sendtox3-epubcheck:5.4.0 --failonwarnings /data/book.epub
FROM eclipse-temurin:21-jre-alpine
ARG EPUBCHECK_VERSION=5.4.0
# GitHub release asset digest for epubcheck-5.4.0.zip (2026-09-15).
ARG EPUBCHECK_SHA256=33350c61038e71dfb3d45a76aed04bf5481e6d5500cb780f6e98db8bbd15a28c
RUN wget -q -O /tmp/ec.zip "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" \
 && echo "${EPUBCHECK_SHA256}  /tmp/ec.zip" | sha256sum -c - \
 && unzip -q /tmp/ec.zip -d /opt \
 && mv "/opt/epubcheck-${EPUBCHECK_VERSION}" /opt/epubcheck \
 && rm /tmp/ec.zip
RUN adduser -D -H -u 10001 epubcheck
USER epubcheck
WORKDIR /data
ENTRYPOINT ["java", "-jar", "/opt/epubcheck/epubcheck.jar"]
