# epubcheck

W3C epubcheck 5.4.0 (BSD-3-Clause) on `eclipse-temurin:21-jre-alpine`; the release zip
is pinned by its SHA-256 and the container runs offline as an unprivileged user.

```sh
docker build -t sendtox3-epubcheck:5.4.0 e2e/epubcheck
e2e/epubcheck/run.sh <dir-with-epubs>     # PASS/FAIL per book, --failonwarnings, exit 0 if all pass
SENDTOX3_DOCKER_TESTS=1 python -m pytest -q tests/test_render_validation.py -k epubcheck
```
