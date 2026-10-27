# iPhone and iPad: sending to the X3

There are three ways to send a web page, a link or some text from an iPhone or iPad
to Send to X3:

| Way | Works where | Feedback | Setup |
|---|---|---|---|
| **A. The Send to X3 shortcut** (recommended) | At home directly over Wi-Fi; elsewhere by email | Notification with the saved title | 5 minutes |
| **B. Home Assistant Companion app** share sheet | Wherever the Companion app reaches your Home Assistant | The Companion app's own "sent" message | None if you use the Companion app |
| **C. Email** from Mail or Gmail | Anywhere | None (check the queue page) | Needs the app's email ingest |

## What you need first

Open the Send to X3 app's web UI (on the app's page in Home Assistant, select **Open
Web UI**) and go to **Setup**. Note:

- **Address**: the ingest URL, for example `http://192.168.1.50:8765/api/v1/ingest`.
  Use the IP address shown there, not `homeassistant.local`: Shortcuts often cannot
  resolve `.local` names.
- **Ingest token**: a long random string. The Setup page shows it as a QR code too
  (point the iPhone camera at it and tap "Copy").
- **Email address** (only if email ingest is set up, see the app's Documentation tab):
  the address the app reads, usually `yourname+secret@gmail.com`.
- The **names of your home Wi-Fi networks**, spelled exactly as in Settings > Wi-Fi
  (capitals matter). If you have a separate 5 GHz network, list both. Curly
  apostrophes and quotes that the keyboard types for you (Smart Punctuation) are fine.

---

## A. The Send to X3 shortcut

### What it does

When you share to it, the shortcut:

1. collects what you shared. From Safari's Share button it takes the page address,
   the page title and any text you had selected. From other apps it takes the link,
   when only a link was shared (or only a domain name such as `example.com/a`, which it
   sends as `https://example.com/a`), or else the text as it is. Run from the Shortcuts
   app or a widget with nothing shared, it uses what is on the clipboard;
2. picks a route. In **auto** mode it sends directly to the Send to X3 app when the
   iPhone is on one of your home Wi-Fi networks, and by email everywhere else.
   **http** always sends directly; **email** always sends by email;
3. **directly**: `POST`s `{"url", "text", "title", "source": "ios-shortcut"}` to the
   ingest URL with the header `Authorization: Bearer <token>`, then shows a
   notification such as "Sent to X3: The Quiet Craft of Electronic Paper". If the app
   refuses (for example a wrong token) you get an alert with the reason;
4. **by email**: sends an email without showing the compose screen, to the ingest
   address, subject `[x3] <page title>`. When there is a link (the page or link
   address, or for text the first `http://` or `https://` link written in it), the
   body is `<link>`, a blank line, `<text>`; otherwise it is just `<text>`. Email
   addresses never count as the link.

The Send to X3 app fetches pages itself, so for a link the shortcut only sends the
address. For text selected in Safari it sends the text and the page address, and the
text is kept with the page as its source. Other text is sent as it is: the app finds
the links in it (see [What gets sent](#what-gets-sent)).

### Install the ready-made shortcut

The repository contains the shortcut as a file:
`shortcuts/dist/Send to X3.shortcut`. iPhones and iPads only open **signed** shortcut
files, and only a Mac signed in to iCloud can sign one. Pick one way:

**1. Sign it on your Mac for yourself** (nothing is uploaded)

```sh
python3 shortcuts/build_shortcut.py --sign people-who-know-me
```

This writes `shortcuts/dist/signed/Send to X3.shortcut`, signed with your iCloud
identity. Such a file opens on your own devices and for people who have you in their
contacts. On the iPhone, turn on **Settings > Apps > Shortcuts > Private Sharing**
(Settings > Shortcuts on iOS 17 and older), then AirDrop the file to the iPhone (or
save it to iCloud Drive and open it in Files).

**2. Sign it for anyone** (uploads the shortcut to Apple)

```sh
python3 shortcuts/build_shortcut.py --sign anyone
```

Apple's signing service signs the file so that any iPhone can open it. Built as
above, the file you upload contains no secrets: the token, addresses and Wi-Fi names
are asked for when the shortcut is added. Values you bake in with the generator's
options are readable by anyone who gets the file, so it refuses `--token` with
`--sign anyone`, and `--email` too (the address's secret `+tag` is what lets mail in)
unless you add `--allow-baked-values` for a file that stays with you; it warns about
`--ingest-url` and `--home-wifi`. Apple's service sometimes answers "error 500"; the
generator retries three times. A file built without `--token` and `--email` can be
given to others.

The Send to X3 Setup page does not offer the shortcut as a download (an unsigned file
does not open on an iPhone); it shows the address and token the setup questions ask
for.

**3. No Mac?** Build it yourself in the Shortcuts app, see
[Build the shortcut yourself](#build-the-shortcut-yourself) below. It takes about ten
minutes.

Once it is on one device, you can share it to your other devices with the Shortcuts
app (long-press it > Share > Copy iCloud Link); the questions are asked again.

### Answer the setup questions

When you open the file, Shortcuts shows **Set Up This Shortcut** with five questions.
Each answer goes into a clearly labelled Text action at the top of the shortcut, so
you can change it later (open the shortcut in the Shortcuts app and edit the text).

| Question | Enter | Example |
|---|---|---|
| Send to X3 address | The Address from the Setup page | `http://192.168.1.50:8765/api/v1/ingest` |
| Send to X3 ingest token | The Ingest token from the Setup page | `Zk3...` (32 characters) |
| Your home Wi-Fi network names | Comma-separated, exact spelling | `Miller Home, Miller Home 5G` |
| Email address the Send to X3 app reads | The ingest email address, or leave empty | `jane+x3k9q@gmail.com` |
| Mode | `auto`, `http` or `email` | `auto` |

The mode may be typed in any capitalization (`Auto`, `HTTP`); `wifi` and `direct` also
mean http, `e-mail` means email, and anything else means auto.

Wi-Fi names are compared exactly (capitals matter), except for what iOS's Smart
Punctuation changes while you type: curly apostrophes and quotes count as straight
ones and a long dash as two hyphens, so `Miller’s Wi-Fi` typed on the iPhone matches a
network named `Miller's Wi-Fi` on the router.

Then tap **Add Shortcut**.

### Use it

- **Safari**: tap the Share button, then **Send to X3** (scroll the app row or the
  action list; tap **Edit Actions** at the bottom of the share sheet to move it up).
  To send only part of a page, select the text first, then tap the Share button in
  the toolbar.
- **Selected text in any app**: select, then **Share...** in the menu, then Send to X3.
- **Other apps** (News readers, Mastodon, Reddit, Mail, Notes...): their Share button.
- **Clipboard**: copy something, then run Send to X3 from the Shortcuts app, a Home
  Screen widget, the Action button or Back Tap (Settings > Accessibility > Touch > Back
  Tap). iOS asks once whether Shortcuts may paste from other apps.

The first time, iOS asks a few permission questions. Answer **Always Allow** to:
"Allow Send to X3 to connect to 192.168.1.50?", "...to send text to ...?",
"...to send email?" and, if asked, the question about your network or location (the
Wi-Fi check needs it). You can review them later in the shortcut's details >
Privacy.

### Modes and the home Wi-Fi check

- A direct send only works on your home network: the ingest URL is a local address. A
  request to it from outside fails and iOS stops the shortcut with "could not connect
  to the server". That is why **auto** checks the Wi-Fi name *before* sending.
- **auto** compares the current Wi-Fi network name with your list. Off Wi-Fi (mobile
  data) the name is empty and the shortcut emails.
- If auto sends email even at home, iOS is not reporting the Wi-Fi name to Shortcuts
  (add Get Network Details to a test shortcut to see what it returns). Set the mode
  to `http` if you mostly share at home, and use the email way when away.
- **email** always emails. Shortcuts sends the email through the Mail app's default
  account, so the Mail app must have an account set up. If you use only Gmail, use
  `http` mode at home and [share to Gmail](#c-email-from-mail-or-gmail) when away.
- Emails go out through your provider, wait in Mail's outbox when you are offline, and
  are picked up by the Send to X3 app the next time it checks the mailbox.

### What gets sent

| You share | `url` | `title` | `text` |
|---|---|---|---|
| A Safari page (Share button) | Page address | Page title | Selected text, if any |
| A link from an app | The link | (empty: the app reads the page title) | (empty) |
| Only a domain name, such as `example.com/a` | `https://example.com/a` | (empty) | (empty) |
| Text from an app or a text selection | (empty) | (empty) | The text |
| Text and a link together (many apps do this) | (empty) | (empty) | Both, one per line |
| Nothing (run from a widget) | The link, if the clipboard holds only a link | (empty) | The clipboard text, otherwise |

The Send to X3 app decides whether to fetch a page or keep the text. A lone link
becomes the article. A Safari selection is kept as a note with the page as its source
(unless it is only the page title). For text, the app looks for web links itself: one
link with a short comment (up to about 30 words) becomes the article with your comment
as a note, a few links with little else become one article each, and anything else is
kept as a note with its first web link as the source. Email addresses (`mailto:`
links) are never fetched.

By email the app gets the same thing: the body starts with the `url` column, or, for
text, with the first `http://` or `https://` link in it, followed by a blank line and
the `text` column. Text without such a link is sent as it is.

### Troubleshooting

| Message | What to do |
|---|---|
| "Send to X3 is not set up" | Open the shortcut and fill in the Text actions at the top. |
| "Send to X3: not saved: invalid or missing ingest token" | Copy the token again from the Setup page. If you changed it in the app options, update the shortcut. |
| "Could not connect to the server" / "The request timed out" | You are not on your home Wi-Fi (set the mode to `auto`), the IP address changed (give Home Assistant a fixed address in your router), or the app is stopped. |
| "A server with the specified hostname could not be found" | Use the IP address instead of a `.local` name. |
| "Send to X3: no email address" | Fill in the email address, or set the mode to `http`. |
| The email is sent but nothing arrives | Check the app's email settings and its log; the sender must be allowed (`allowed_senders`). |
| Shortcut is missing from the share sheet | Open the shortcut's details (the (i) button) and check Show in Share Sheet; some apps share only images or files, which the shortcut does not accept. |
| "Nothing to send" | The share contained no text or link (for example only an image). |
| A long selection fails with a message about sharing a large amount of data | Turn on Settings > Apps > Shortcuts > Advanced > **Allow Sharing Large Amounts of Data**. |

---

## Build the shortcut yourself

These steps rebuild the same shortcut by hand in the Shortcuts app (iOS 17 to 26; the
wording of a few buttons differs slightly between versions). Add each action with the
search field at the bottom ("Search for apps and actions") and tap the blue
placeholders to fill them in. To pick a variable, tap a field, then **Select Variable**
(or the variable bar above the keyboard).

The generated file also normalizes the spelling of the mode and stops early with a
message when a setting is missing; the recipe leaves those out to stay short.

### 1. Create it and accept shares

1. Shortcuts > **+**. Tap the name at the top > **Rename** > `Send to X3`. Optionally
   choose an icon.
2. Open the shortcut's details (the **(i)** button) and turn on **Show in Share
   Sheet**. Close the details.
3. At the top of the editor, tap the input types in "Receive **...** input from Share
   Sheet": select only **Articles**, **Rich Text**, **Safari web pages**, **Text** and
   **URLs**.
4. In the same block, tap "If there's no input: **Continue**" and choose **Get
   Clipboard**.

### 2. Settings (top of the shortcut)

5. **Comment**: `Send to X3 settings: edit the Text actions below.`
6. **Text**: your ingest URL. Then **Set Variable**: name `IngestURL`, to **Text**.
7. **Text**: your ingest token. **Set Variable** `IngestToken`.
8. **Text**: your home Wi-Fi names, comma-separated. **Set Variable** `HomeWiFi`.
9. **Text**: your ingest email address (or leave it empty). **Set Variable** `IngestEmail`.
10. **Text**: `auto` (lowercase). **Set Variable** `Mode`.

Optional: on the details screen, **Setup** > **Add Question** lets you turn steps 6 to
10 into import questions if you share the shortcut.

### 3. What was shared

11. **If**: tap the first placeholder, choose **Shortcut Input**; tap the Shortcut
    Input token again, tap **Type** and choose **Safari web page**; condition **has
    any value**.
12. Inside the If (above "Otherwise"):
    - **Get Details of Safari Web Page**: **Page URL** of **Shortcut Input**, then
      **Set Variable** `SharedURL`.
    - **Get Details of Safari Web Page**: **Name** of Shortcut Input, **Set Variable**
      `SharedTitle`.
    - **Get Details of Safari Web Page**: **Page Selection** of Shortcut Input, **Set
      Variable** `SharedText`.
    - **Set Variable** `SharedLink` to the variable **SharedURL** (the link an email
      starts with).
13. Inside "Otherwise":
    - **Get Text from Input**: **Shortcut Input**, **Set Variable** `SharedText`.
    - **Get URLs from Input**: the variable **SharedText**.
    - **Replace Text**: in **URLs**, find
      `^(?:(?!https?://).*\n)*(https?://.*)?[\s\S]*$`, replace with `$1`; **Show
      More**, turn on **Regular Expression**. This keeps the first web link (Get URLs
      from Input also returns email addresses, as `mailto:` links). **Set Variable**
      `SharedURL`.
    - **Text** (leave empty), **Set Variable** `SharedTitle`.
14. Still inside "Otherwise": **If** `SharedText` **is** `SharedURL` (pick the variable
    in the value field); inside (only a link was shared): **Text** (empty), **Set
    Variable** `SharedText`; **Set Variable** `SharedLink` to **SharedURL**.
    **Otherwise**:
    - **Replace Text**: in **SharedText**, find
      `^\s*((?:[a-z0-9-]+\.)+[a-z]{2,63}(?::[0-9]{1,5})?(?:[/?#]\S*)?)\s*$`, replace with
      `http://$1` (Regular Expression on).
    - **If** **Updated Text** **is** `SharedURL` (only a domain name such as
      `example.com` was shared): **Replace Text** in **SharedText** with the same Find,
      replace with `https://$1`, **Set Variable** `SharedURL`; **Text** (empty), **Set
      Variable** `SharedText`; **Set Variable** `SharedLink` to **SharedURL**.
    - **Otherwise** (a text: the app finds its links): **Text** (empty), **Set
      Variable** `SharedLink`; **If** **SharedText** **contains** `SharedURL`: **Set
      Variable** `SharedLink` to **SharedURL**; End If. **Text** (empty), **Set
      Variable** `SharedURL`.
    - End If.

    End If. Leave the outer "End If" as it is.

### 4. Choose the route

15. **Get Network Details**: **Wi-Fi**, detail **Network Name**. **Set Variable**
    `CurrentWiFi`.
16. **Replace Text**: in the text field type a comma, insert the variable **HomeWiFi**,
    type another comma (so it reads `,HomeWiFi,`). Find: `\s*[,;\n][\s,;\n]*`,
    Replace with: `,`. Tap **Show More** and turn on **Regular Expression**. **Set
    Variable** `HomeWiFiList`.

    If a home network name contains an apostrophe, quotes or two hyphens, iOS's Smart
    Punctuation may have curled them when you typed step 8. The generated shortcut
    straightens both names before comparing. To do the same, add three **Replace Text**
    actions (Regular Expression on) after step 15, the first one on **CurrentWiFi**
    and each next one on the previous **Updated Text**, then **Set Variable**
    `CurrentWiFi` again; and the same three after step 16 for `HomeWiFiList`:

    | Find | Replace with |
    |---|---|
    | `[‘’‚‛ʼ]` | `'` |
    | `[“”„‟]` | `"` |
    | `—` | `--` |

17. **Text**: `email`, **Set Variable** `Route`.
18. **If** `Mode` **is** `http`: inside, **Text** `http`, **Set Variable** `Route`. End If.
19. **If** `Mode` **is** `auto`: inside, **If** `HomeWiFiList` **contains**
    `,CurrentWiFi,` (a comma, the variable CurrentWiFi, a comma); inside that,
    **Text** `http`, **Set Variable** `Route`. End both Ifs.

### 5. Send

20. **If** `Route` **is** `http`:
    - **Get Contents of URL**: URL: the variable **IngestURL**. Tap **Show More**:
      Method **POST**; **Headers**: add a header, key `Authorization`, value
      `Bearer ` (with a space) followed by the variable **IngestToken**; **Request
      Body** **JSON**: add four **Text** fields: `url` = SharedURL, `text` =
      SharedText, `title` = SharedTitle, `source` = `ios-shortcut`.
    - **Get Dictionary Value**: **Value** for key `message` in **Contents of URL**.
    - **If** **Dictionary Value** **has any value**: **Show Notification**: the
      Dictionary Value. **Otherwise**: **Get Dictionary Value** for `error` in
      Contents of URL, then **Show Alert** with that value. End If.
21. **Otherwise** (of the Route If):
    - **Text**: type `<`, insert the variable **SharedLink**, type `>`. **If** that
      **Text** **is** `<>` (there is no link): **Text** with the variable SharedText,
      **Set Variable** `EmailBody`. **Otherwise**: **Text** with SharedLink, two new
      lines, SharedText, **Set Variable** `EmailBody`. End If.
    - **Send Email**: Recipients: the variable **IngestEmail**; Subject: `[x3] `
      followed by SharedTitle; Message: the variable **EmailBody**. Tap **Show More**
      and turn **Show Compose Sheet** off.
    - **Show Notification**: `Emailed to Send to X3`.
22. End If. Tap **Done**.

Test it from Safari at home (you should see "Sent to X3: <title>") and, with Wi-Fi off,
on mobile data (an email should go out).

---

## B. Home Assistant Companion app share sheet

If the Home Assistant Companion app is on the iPhone and signed in, you can share to
Home Assistant directly; Send to X3 listens for these shares (`ha_share_enabled`,
on by default).

1. Share a page, a link or text and choose **Home Assistant** in the share sheet (tap
   **More** and turn it on the first time).
2. The Companion app shows a text box, usually pre-filled with the page title or the
   selected text. Leave it as it is, or write your own words over the page title,
   and tap **Send**. What you write is kept: with a short comment (up to about 30
   words) the app fetches the page and adds your comment to it; a longer text is kept
   as a note with the page as its source.
3. The Companion app fires a `mobile_app.share` event in Home Assistant; Send to X3
   picks it up and queues the item. Check the Send to X3 queue page if you want to
   see the title.

This works wherever the Companion app can reach your Home Assistant (at home, or away
if you use Home Assistant Cloud or your own remote access), and needs no token or
shortcut. It gives no confirmation from Send to X3 itself, and shares made while the
Send to X3 app is restarting are lost (Home Assistant does not replay events). After
Home Assistant itself restarts, Send to X3 is listening again within about five
seconds of Home Assistant coming back.

---

## C. Email from Mail or Gmail

With email ingest set up (see the app's Documentation tab: a dedicated Gmail account
with an app password works well), anything you email to the ingest address arrives on
the X3:

- **Share a page**: Share > **Mail** (or **Gmail**). Put the ingest address in To, keep
  the link in the body and send. The subject is used as the title (a leading `[x3]`,
  `Fwd:` or `Re:` is removed); leave it empty to use the page's own title.
- **Selected text**: select, Share... > Mail, send. Long text is kept as a note.
- **Newsletters and articles you received**: forward them to the ingest address.
- **PDFs**: attach them; each PDF, TXT or HTML attachment becomes its own item.

Tips:

- Save the ingest address as a contact named "Send to X3" so it autocompletes.
- The app only accepts mail that passes your mailbox's sender checks (`require_dmarc`)
  and comes from an address in `allowed_senders` or is sent to the plus address
  (`imap_plus_tag`). When neither is set, no mail is read at all. Send from an
  address you listed, or to the plus address.
- iOS's "Sent from my iPhone" signature is removed automatically.
