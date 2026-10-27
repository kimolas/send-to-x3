"""In-process fakes of the external systems Send to X3 talks to (no network needed).

- :mod:`tests.fakes.crosspoint_server`: the CrossPoint File Transfer web server
  (``/api/status``, ``/api/files``, ``/mkdir``, ``/upload``, ``/delete``,
  ``/api/settings``, ``/api/plugin-fs``, WebDAV ``PUT``/``MKCOL``/``PROPFIND``), for
  firmware 1.6.5 and 2.0, backed by a temp folder used as the SD card.
- :mod:`tests.fakes.ha_ws`: Home Assistant's websocket API as seen through the
  Supervisor proxy (auth, ``subscribe_events``, ``ping``, device registry) that can
  push ``mobile_app.share`` events.

Both bind 127.0.0.1 on a free port and can be stopped/restarted on the same port to
emulate a device leaving File Transfer mode or Home Assistant restarting.

Import them as ``from fakes.crosspoint_server import FakeCrossPoint`` and
``from fakes.ha_ws import FakeHomeAssistant``: pytest puts ``tests/`` (the folder of
``tests/conftest.py``) on ``sys.path``, so this works from any test folder.
"""
