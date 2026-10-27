# Phone shortcuts

Generators and checks for the phone side of Send to X3. User guides:
[docs/phone-ios.md](../docs/phone-ios.md) and [docs/phone-android.md](../docs/phone-android.md).

| Path | What |
|---|---|
| `build_shortcut.py` | Generates the iOS/macOS Shortcut "Send to X3" (standard library only). |
| `dist/Send to X3.shortcut` | Generated, **unsigned** binary plist (no secrets). Commit it. |
| `dist/Send to X3.xml` | The same plist as XML, for review and diffs. |
| `dist/signed/` | Signed copies (`--sign`). Not committed: a `people-who-know-me` file carries your iCloud identity. |
| `build_android.py` | Generates the HTTP Shortcuts (Android) import file, with `send_to_x3/sendtox3/phone.py` (the code the app's Setup page uses for its prefilled download). |
| `android/http-shortcuts-send-to-x3.json` | Generated import file (no secrets). Commit it. |
| `tools/simulate.py` | Interprets the generated shortcut's actions in Python (logic tests). |
| `tools/wfcheck.m` | macOS: loads a shortcut with Apple's WorkflowKit and reports how it decodes and evaluates. |
| `tools/http_shortcuts_check.py`, `tools/http_shortcuts_check/` | Docker: runs HTTP Shortcuts' own import code on the Android file. |

## Regenerate

```sh
python3 shortcuts/build_shortcut.py        # dist/Send to X3.shortcut + .xml
python3 shortcuts/build_android.py         # android/http-shortcuts-send-to-x3.json
```

Output is deterministic (fixed UUIDs, sorted keys); `tests/test_shortcuts.py` fails
when the committed files are stale. Both generators take `--ingest-url`, `--token`,
`--email` (and iOS `--home-wifi`, `--mode`, `--name`) to bake values into a personal
copy; leave the token and the email address out of anything you share.

## Sign (macOS, signed in to iCloud)

```sh
python3 shortcuts/build_shortcut.py --sign people-who-know-me   # local, nothing uploaded
python3 shortcuts/build_shortcut.py --sign anyone               # uploads to Apple's signing service
```

Both run `shortcuts sign` and check that the result is an Apple Encrypted Archive
(`AEA1`). `anyone` retries Apple's intermittent HTTP 500 three times. It is refused
together with `--token`, and with `--email` unless `--allow-baked-values` is given
(the address's `+tag` is the secret that lets mail in, and an `anyone` file is meant
to be passed around); `--ingest-url` and `--home-wifi` only give a warning. A
`people-who-know-me` file only opens on the signer's devices and for people who have
the signer in their contacts, with Private Sharing on.

The app's Setup page does not serve a shortcut file: iPhones open only signed files
and signing needs a Mac, so it shows `IOS_SHORTCUT_NOTE` from
`send_to_x3/sendtox3/phone.py`, which points to docs/phone-ios.md. For Android it
serves the HTTP Shortcuts file prefilled with the server's address and ingest token
(`GET /admin/setup/android.json`, `phone.android_import_json`).

## Validate

```sh
plutil -lint "shortcuts/dist/Send to X3.shortcut"
python -m pytest -q tests/test_shortcuts*.py                       # structure, logic, API contract, macOS checks
SENDTOX3_SHORTCUTS_SIGN=1 python -m pytest -q tests/test_shortcuts_macos.py           # + local signing
SENDTOX3_DOCKER_TESTS=1 SENDTOX3_NETWORK_TESTS=1 python -m pytest -q tests/test_shortcuts_android_import.py
```

`tools/wfcheck.m` (built by the macOS tests with `clang`) uses WorkflowKit, the
framework the Shortcuts app reads files with, in a throwaway process: nothing is
imported into a Shortcuts library. It confirms that every parameter decodes into the
expected parameter state, that the Import Questions round-trip through
`WFWorkflowImportQuestion` unchanged, that the no-input behavior is Get Clipboard, and
that import migrations change nothing. It also evaluates the If conditions and
resolves parameters with given variable values, which pinned down two behaviors the
shortcut depends on: text comparisons are case-sensitive (so the mode is normalized
first) and an empty text item counts as "has any value" (so emptiness is tested as
`"<" + value + ">" is "<>"`). Its `regex` command runs a pattern through
NSRegularExpression, the ICU engine behind Replace Text, so the tests check that every
Replace Text pattern gives the same result there as in `tools/simulate.py`.

What it cannot do: run the whole shortcut. Most actions (Text, Set Variable, Get
Contents of URL, Send Email...) live in ActionKit, which refuses to load outside
Apple's own processes, and `shortcuts run` only runs shortcuts installed in the
library. A full run therefore needs a device test: import the signed file, then share
a Safari page at home (expect "Sent to X3: <title>"), with a text selection, from
another app, with Wi-Fi off (expect an email), and with a wrong token (expect an
alert).

## Format notes

Parameter keys come from Apple's gallery shortcuts on macOS 26.5, WorkflowKit's own
action definitions, and the Cherri compiler / shortcuts-js sources (both decompile
real shortcuts). Import Questions use `{ActionIndex, Category: "Parameter",
DefaultValue, ParameterKey, Text}`, which WorkflowKit parses and writes back
identically. If conditions are written in the single-condition form
(`WFCondition`/`WFInput`/`WFConditionalActionString`) that every Shortcuts version
reads; current versions turn it into their condition table on load.

The HTTP Shortcuts file follows the app's export format version 92 (compatible with
90). Variable placeholders are `{{<variable id>}}`, and the app only recognizes IDs
shaped like random (version 4) UUIDs, so the generator derives stable v4-shaped IDs.
