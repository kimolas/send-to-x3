# Extraction test fixtures

Every file in this folder was written for the Send to X3 test suite and is released
under CC0. The pages imitate the *markup* of common sites (news CMS boilerplate,
MediaWiki, Substack, GitHub's README API, Bluesky and X APIs, bot-check interstitials)
but all names, events and text are fictional. No third-party page content is stored here.

Encoded fixtures (koi8-r, Shift_JIS, cp1252, mislabelled UTF-8) are generated inside
`tests/test_extract_fetch.py` and `tests/test_extract_html.py` from Unicode strings, so
the bytes under test are visible in the test source.
