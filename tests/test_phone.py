"""sendtox3.phone: the HTTP Shortcuts import file the Setup page serves, and the iOS note."""

from __future__ import annotations

import json

import pytest
from sendtox3 import phone
from test_shortcuts import ba, render_android_request


def test_generator_reuses_the_package_module():
    """shortcuts/build_android.py writes exactly what the app serves when nothing is filled in."""
    assert ba.build_http_shortcuts is phone.build_http_shortcuts and ba.AndroidConfig is phone.AndroidConfig
    assert ba.DEFAULT_OUT.read_bytes() == phone.http_shortcuts_bytes() == phone.android_import_json("", "")


@pytest.mark.parametrize("base_url", ["http://192.168.1.50:8765", "http://192.168.1.50:8765/",
                                      " http://192.168.1.50:8765/api/v1/ingest "])
def test_android_import_json_is_prefilled(base_url):
    data = json.loads(phone.android_import_json(base_url, "tok-123"))
    values = {v["key"]: v["value"] for v in data["variables"]}
    assert values == {"sendtox3_url": "http://192.168.1.50:8765/api/v1/ingest", "sendtox3_token": "tok-123",
                      "sendtox3_email": "", "sendtox3_text": "", "sendtox3_title": ""}
    # Same file as the generator's apart from the values: same ids, so importing it
    # updates an existing "Send to X3" shortcut instead of adding a second one.
    generic = phone.build_http_shortcuts()
    for v in data["variables"]:
        v["value"] = ""
    assert data == generic
    # and the request it makes goes to this server with this token
    url, headers, body = render_android_request(json.loads(phone.android_import_json(base_url, "tok-123")),
                                                text="hello", title="", values={})
    assert url == "http://192.168.1.50:8765/api/v1/ingest" and headers["Authorization"] == "Bearer tok-123"
    assert json.loads(body) == {"text": "hello", "title": "", "source": "android-http-shortcuts"}


def test_android_import_json_email_and_validation():
    data = json.loads(phone.android_import_json("http://10.0.0.5:8765", "t", email="me+x3@example.com"))
    assert {v["key"]: v["value"] for v in data["variables"]}["sendtox3_email"] == "me+x3@example.com"
    with pytest.raises(ValueError):
        phone.android_import_json("http://10.0.0.5:8765", "a\nb")


def test_ios_shortcut_note_points_to_the_docs():
    note = phone.IOS_SHORTCUT_NOTE
    assert "signed" in note and "Mac" in note and "docs/phone-ios.md" in note
    assert "\n" not in note and len(note) < 500
