# Android: sending to the X3

There are three ways to send a web page, a link or some text from an Android phone to
Send to X3:

| Way | Works where | Feedback | Setup |
|---|---|---|---|
| **A. HTTP Shortcuts** (recommended) | At home directly over Wi-Fi; away it prepares an email | "Sent to X3: <title>" | 5 minutes |
| **B. Home Assistant Companion app** share | Wherever the Companion app reaches your Home Assistant | The Companion app's own message | None if you use the Companion app |
| **C. Share to Gmail** (or any email app) | Anywhere | None (check the queue page) | Needs the app's email ingest |

## What you need first

Open the Send to X3 app's web UI (on the app's page in Home Assistant, select **Open
Web UI**) and go to **Setup**. Note:

- **Address**: the ingest URL, for example `http://192.168.1.50:8765/api/v1/ingest`.
- **Ingest token**: a long random string (also shown as a QR code: scan it with the
  camera app and copy it).
- **Email address** (only if email ingest is set up): usually `yourname+secret@gmail.com`.

---

## A. HTTP Shortcuts

[HTTP Shortcuts](https://http-shortcuts.rmy.ch) is a free, open-source app (Google Play
and F-Droid) that sends HTTP requests and can receive shares. The file
`shortcuts/android/http-shortcuts-send-to-x3.json` in this repository sets it up. It
was checked against the app's own import code at version 4.9.0.

### Install

1. Install **HTTP Shortcuts** (`ch.rmy.android.http_shortcuts`) from Google Play or
   F-Droid.
2. On the phone, open the Send to X3 Setup page and tap
   **Download HTTP Shortcuts file**. That file already has the app's address and
   ingest token filled in. (Or put the generic `shortcuts/android/http-shortcuts-send-to-x3.json` from the
   repository on the phone: download it, email it to yourself, or copy it over USB.)
3. In HTTP Shortcuts open the menu (three dots) > **Import / Export** > **Import from
   File** and pick the file. (If the file is on a web server you can reach, **Import
   from URL** works too.) Importing adds a "Send to X3" category; your other
   shortcuts stay as they are.
4. Menu > **Global Variables** and check or fill in (the Setup page's file has the
   first two already):

   | Variable | Value |
   |---|---|
   | `sendtox3_url` | The Address from the Setup page |
   | `sendtox3_token` | The Ingest token from the Setup page |
   | `sendtox3_email` | Optional: the ingest email address, for sending when away from home |

   These three are marked "Exclude stored value from exports": importing an updated
   file later keeps what you entered, and your own HTTP Shortcuts backups do not
   contain the token.
5. On Android 17 and newer, allow access to the local network when asked (or turn on
   menu > Settings > **Troubleshooting** > **Allow Access to Local Network**).
6. Optional: long-press the shortcut > **Place on Home Screen**. On Android 11 and
   newer it also shows up as a Direct Share target at the top of the share sheet.

### Use it

- **From Chrome or any app**: Share > **HTTP Shortcuts** (or the "Send to X3" Direct
  Share target). A toast shows "Sent to X3: <title>".
- **Typing or pasting**: tap the shortcut (in the app or on the Home Screen). It asks
  for the text or link.
- **Away from home**: the phone cannot reach the app, so the shortcut opens your email
  app with a message to `sendtox3_email` already filled in (subject `[x3] <title>`,
  body the shared text). Tap Send. Without `sendtox3_email` it tells you that the app
  could not be reached.
- **Wait for connection when offline** is on: if the phone has no network at all, the
  app keeps the share and sends it when a connection comes back. Android only reports
  this case for host names it cannot resolve, so with an IP address in
  `sendtox3_url` a share made offline goes straight to the email fallback.

### What it sends

`POST <sendtox3_url>` with `Authorization: Bearer <sendtox3_token>`,
`Content-Type: application/json` and

```json
{"text": "<shared text>", "title": "<shared subject>", "source": "android-http-shortcuts"}
```

Chrome shares the page address as the text and the page title as the subject; other
apps share text, often with a link in it. The Send to X3 app decides whether to fetch
the page or keep the text: a lone link becomes the article, and so does a link with a
short text (up to about 30 words, kept as a comment unless it is the page title); a
few links with little else become one article each; anything else is kept as a note
with its first web link as the source. The shared values are inserted with HTTP Shortcuts' **JSON encode** option, so
quotes, backslashes and line breaks in the shared text cannot break the request.

### Troubleshooting

| Message | What to do |
|---|---|
| "Send to X3 is not set up" | Fill in `sendtox3_url` and `sendtox3_token` in Global Variables. |
| "Send to X3: not saved: invalid or missing ingest token" | Copy the token again from the Setup page. |
| "The address answered HTTP ... but is not the Send to X3 ingest address" | `sendtox3_url` points at something else (for example Home Assistant itself on port 8123). Use the Address from the Setup page. |
| "Could not reach the Send to X3 app" | Not on the home Wi-Fi, the Pi's IP address changed (give it a fixed address in the router), the app is stopped, or Android blocked local network access (step 5). |
| HTTP Shortcuts is missing from the share sheet | Some apps only share files or images; this shortcut accepts text and links. Send PDFs by email. |

### Build it by hand

If you prefer not to import the file, create the same shortcut in HTTP Shortcuts:

1. Menu > **Global Variables** > **+**:
   - `sendtox3_url`, `sendtox3_token`, `sendtox3_email`: type **Static Variable**,
     value as in the table above; tick **Exclude stored value from exports**.
   - `sendtox3_text`: type **Text Input**, dialog title `Send to X3`, message
     `Text or link to send to the X3`, tick **Multiline**. Under the advanced
     settings tick **Allow Receiving Value from Share Dialog** with **Data to Receive
     from Sharing** = **Text only**, and tick **JSON encode**.
   - `sendtox3_title`: type **Static Variable**, empty value, **Allow Receiving Value
     from Share Dialog** with **Title only**, and **JSON encode**.
2. Back on the main screen, **+** > a regular HTTP shortcut named `Send to X3`:
   - **Basic Request Settings**: Method **POST**; URL: insert the variable
     `sendtox3_url` with the `{}` button.
   - **Request Headers**: `Authorization` = `Bearer ` followed by the variable
     `sendtox3_token`.
   - **Request Body**: Request Body Type **Custom Text**, Content-Type
     `application/json`, and this body, where `[sendtox3_text]` and
     `[sendtox3_title]` stand for the variables inserted with the `{}` button (keep
     the quotes around them):

     ```
     {"text": "[sendtox3_text]", "title": "[sendtox3_title]", "source": "android-http-shortcuts"}
     ```
   - **Response Handling**: Display Type **Toast Popup**; On Success and On Failure:
     **Show nothing (run silently)** (the scripts below show the messages).
   - **Scripting**: paste the three scripts below.
   - **Trigger & Execution Settings**: tick **Wait for connection when offline** and
     **Show as app shortcut on launcher**. Under **Advanced Technical Settings** set the
     Timeout to 30 seconds.

**Run before Execution**

```js
// Send to X3: stop early with a clear message when the settings are missing.
if (!getVariable('sendtox3_url') || !getVariable('sendtox3_token')) {
  showDialog('Open HTTP Shortcuts, menu > Global Variables, and fill in sendtox3_url (the ingest address) and '
    + 'sendtox3_token (the ingest token) from the Setup page of the Send to X3 app.', 'Send to X3 is not set up');
  abort();
}
```

**Run on Success**

```js
// Show the title the Send to X3 app saved ("Sent to X3: <title>"). Anything that is
// not its JSON answer (a login page, another server) counts as a failure.
let reply = null;
try {
  reply = JSON.parse(response.body);
} catch (e) {}
if (!reply || reply.ok !== true) {
  abortAndTreatAsFailure('not a Send to X3 answer');
}
showToast(reply.message || 'Sent to X3');
```

**Run on Failure**

```js
// The shared values are JSON-encoded (see the variables); undo that for the email.
function plain(value) {
  try {
    return JSON.parse('"' + value + '"');
  } catch (e) {
    return value;
  }
}
if (response) {
  // The app answered with an error (wrong token, nothing to send, ...), or something
  // else answered at that address.
  let error = null;
  try {
    error = JSON.parse(response.body).error;
  } catch (e) {}
  if (!error) {
    error = 'The address answered HTTP ' + response.statusCode
      + ' but is not the Send to X3 ingest address. Check sendtox3_url in Global Variables.';
  }
  showDialog(htmlEncode(error), 'Send to X3: not saved');
} else {
  // No answer at all: probably not on the home Wi-Fi. Fall back to email, if set up.
  const to = getVariable('sendtox3_email');
  if (to) {
    const title = plain(getVariable('sendtox3_title'));
    const text = plain(getVariable('sendtox3_text'));
    openUrl('mailto:' + to + '?subject=' + encodeURIComponent('[x3] ' + title)
      + '&body=' + encodeURIComponent(text));
  } else {
    showDialog(htmlEncode('Could not reach the Send to X3 app (' + networkError + '). '
      + 'Are you on your home Wi-Fi? To send from anywhere, set sendtox3_email in Global Variables.'), 'Send to X3');
  }
}
```

To regenerate the import file with your values already filled in (for your own phone
only; the file then contains your token):

```sh
python3 shortcuts/build_android.py --out ~/Downloads/send-to-x3.json \
  --ingest-url http://192.168.1.50:8765/api/v1/ingest --token <ingest token> --email <ingest address>
```

---

## B. Home Assistant Companion app share

If the Home Assistant Companion app is installed and signed in, share to it directly;
Send to X3 listens for these shares (`ha_share_enabled`, on by default).

1. Share a page, a link or text and choose **Home Assistant** in the share sheet.
2. The Companion app fires a `mobile_app.share` event in Home Assistant with the
   shared text or link and the subject (Chrome's page title). Send to X3 picks it up
   and queues the item; the Companion app confirms that it was sent to Home Assistant.

This works wherever the Companion app can reach your Home Assistant (at home, or away
with Home Assistant Cloud or your own remote access) and needs no token. Send to X3
gives no direct confirmation (look at its queue page), and shares made while the app
is restarting are lost because Home Assistant does not replay events. After Home
Assistant itself restarts, Send to X3 is listening again within about five seconds of
Home Assistant coming back.

---

## C. Share to Gmail or another email app

With email ingest set up (see the app's Documentation tab), anything you email to the
ingest address arrives on the X3:

- **A page**: Share > **Gmail**. To: the ingest address. Gmail fills the subject with
  the page title and the body with the link; tap Send. The subject becomes the title
  (`[x3]`, `Fwd:` and `Re:` prefixes are removed).
- **Selected text**: select, **Share** > Gmail, send.
- **Newsletters and articles you received**: forward them to the ingest address.
- **PDFs**: attach them; each PDF, TXT or HTML attachment becomes its own item.

Save the ingest address as a contact named "Send to X3" so it autocompletes. The app
only reads mail that comes from an address in `allowed_senders` or is sent to the
plus address (`imap_plus_tag`); when neither is set, no mail is read at all.
