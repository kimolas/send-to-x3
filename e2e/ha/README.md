# Home Assistant packaging checks

Tools that check the `send_to_x3/` app the way Home Assistant's Supervisor sees
it. Unit-level packaging checks (no network, no docker) are in
`tests/test_packaging.py` and run with the normal test suite.

| Tool | What it checks | Needs |
|---|---|---|
| `validate_config.sh` / `validate_config.py` | `repository.yaml`, app discovery, `config.yaml`, translations, option sets and the configuration form, validated by the Supervisor's own code (pinned release `2026.10.4`); then reads every accepted `options.json` back with `sendtox3.config.load_options`; prints the security rating | git, Python 3.14, network on the first run |
| `tests/test_packaging_docker.py` | builds the image, checks imports, `tesseract`, labels, s6 exit codes, no runtime bytecode, and `GET /healthz` from a running container | docker |
| `devcontainer_install.sh` | installs the app as a local app in a real Supervisor and Core (official `ghcr.io/home-assistant/devcontainer:5-apps`, stable channel): build, install, start, `options.json`, `/healthz` on the host port, token required off ingress, the web UI, a form POST and a 20 MB upload through real ingress (Core, Supervisor, app), and a `mobile_app.share` event becoming an item | docker with privileged containers, ~6 GB disk, network |
| `make_icons.py` | regenerates `send_to_x3/icon.png` and `logo.png` | Pillow |

## Commands

From the repository root:

```sh
# Supervisor schema validation (clones the Supervisor and builds a venv in WORK_DIR)
WORK_DIR=/tmp/sendtox3-ha PYTHON=python3.14 e2e/ha/validate_config.sh

# Image build + container smoke test, native arch (arm64 on Apple Silicon)
SENDTOX3_DOCKER_TESTS=1 python -m pytest -q tests/test_packaging_docker.py
# the other arch under emulation
SENDTOX3_DOCKER_TESTS=1 SENDTOX3_DOCKER_PLATFORM=linux/amd64 python -m pytest -q tests/test_packaging_docker.py

# Full install in a real Supervisor (5-15 minutes the first time)
WORK_DIR=/tmp/sendtox3-ha e2e/ha/devcontainer_install.sh
KEEP=1 WORK_DIR=/tmp/sendtox3-ha e2e/ha/devcontainer_install.sh   # keep it; HA at http://127.0.0.1:7123
e2e/ha/devcontainer_install.sh --down                              # remove container and volumes
```

`WORK_DIR` defaults to `e2e/work/ha`, which is gitignored. Keep it outside a
synced folder (Google Drive, iCloud) if the repository lives in one.

## Notes

- The Supervisor treats every `config.yaml`, `config.yml` or `config.json`
  outside hidden folders and `rootfs/` as an app. Never add files with those names
  elsewhere in this repository; `tests/test_packaging.py` fails if you do.
- There is no `apparmor.txt` yet. Docker Desktop's kernel has no AppArmor, so a
  profile cannot be exercised here; an untested profile could block the app on a
  real Home Assistant OS host. Add one only after testing it on HA OS (it raises the
  security rating from 7 to 8).
