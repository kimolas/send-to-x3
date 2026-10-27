# Send to X3

Send to X3 turns web pages, pasted text, emails and PDFs into clean, image-free
reading files and gets them onto an Xteink X3 running stock CrossPoint firmware.
Nothing on the X3 is modified, and nothing needs to be reachable from the internet.

This page is the configuration reference. Step-by-step guides (iPhone Shortcut,
Android, Gmail filter, X3 setup) are in the `docs/` folder of the app repository.

## Getting started

1. Install the app and start it. Leave every option at its default for now.
2. Select **Open Web UI**. The web UI shows your tokens, setup steps with QR codes,
   the reading queue, and a form to paste text or a link.
3. Pick how your X3 receives items (you can use more than one):

| X3 firmware | How items arrive | What to set up |
|---|---|---|
| CrossPoint 2.x | **Plugin**: open *Send to X3* on the X3 and select the first row, **Get 3 new**, to download everything new as one book (see *Fetching new items* below) | Turn on plugins in the X3 settings, then install the plugin from the web UI (download the SD-card bundle, or push it to the X3 while it shows File Transfer) |
| CrossPoint 1.6.5 or later | **OPDS**: open the OPDS browser on the X3 and download **Get 3 new** (everything new) or single items | Add an OPDS server on the X3 with URL `http://<Home Assistant IP>:8765/opds`, user name `x3`, password = the device token |
| Any | **LAN push**: new items are uploaded while the X3 shows its File Transfer screen | Set `device_host` to the X3's IP address |

4. Send something: from the Home Assistant Companion app share sheet, an iPhone
   Shortcut or Android HTTP shortcut (share API), by email (see `imap_enabled`), or
   with the paste form in the web UI.

Use the IP address of your Home Assistant host in URLs for the X3. The X3 may not
resolve `homeassistant.local`.

### Fetching new items (CrossPoint 2.x)

Nothing arrives on the X3 by itself. When you want your reading, open **Home >
Plugins > Send to X3** and press **Confirm** on the first row, **Get 3 new · Oct 26
14.05**: everything you shared since the last fetch is saved as one book in
`/Inbox` (a single item keeps its own title inside the book). The rows below are
everything you shared, newest first, to download one at a time. When nothing is
new, the first row reads **Nothing new right now**.

The X3 reports each saved download the next time it joins Wi-Fi: after you go back to
Home (the X3 restarts) and open **Send to X3** again, before the list loads. Only then
do the items count as delivered. Meanwhile the web UI's queue shows **sent ..., not
confirmed yet** and the first row reads **Nothing new right now**, so a habitual
Confirm never replaces the book you are reading. A cancelled or failed download is
offered again; a fetch that is never reported comes back after two hours under a new
name. Two fetches never get the same file name (a repeat ends in ` (2)`).

`auto_delivery` (off by default) also delivers new items with no taps when the X3
goes to sleep on your home Wi-Fi. It joins Wi-Fi at every sleep, and needs a
CrossPoint build newer than 2.0.0rc (with develop commit facaa87, "Load WiFi
settings from file before sleep mode"); on 2.0.0rc items arrive the next time the
X3 uses Wi-Fi. Reinstall the plugin after changing it.

## Network

The app listens on port **8765** (plain HTTP). It serves the share API, the OPDS
feed, the X3 plugin endpoints, `/admin` (needs the admin token) and `/healthz`.
Everything except `/healthz` needs a token. Keep this port on your home network:
do not forward it on your router.

The web UI inside Home Assistant (**Open Web UI**, or the sidebar) goes through
Home Assistant's ingress and needs no token.

To use a different host port, change it in the **Network** section of the
Configuration tab and restart the app: it reads the new host port from Home
Assistant by itself. If you set `server_host`, include the port
(`<IP>:<new port>`). Inside the container the app always listens on 8765, because
Home Assistant's panel and the watchdog connect there. After a change, reinstall
the X3 plugin (or download the bundle again) so it uses the new address.

## Configuration

Leave a token empty to have one generated on first start; it is kept in the app's
data and shown in the web UI. Restart the app after you change options.

If you set a token yourself, use letters, digits, `-` and `_`, like the generated
tokens. Other characters can break the links that carry a token (`?token=...`):
`&`, `#`, `+` or `%`, for example, would have to be URL-encoded there.

The device token is checked: it may contain only printable ASCII characters other
than the space (letters, digits and punctuation), and none of `{`, `}`, `"` or `\`.
The X3 plugin cannot use other characters, so with such a token the web UI refuses
to build the plugin and the Setup page shows a warning. Letters and digits are
easiest to type into the X3's OPDS settings.

### General

| Option | Default | What it does |
|---|---|---|
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. Use `debug` when you report a problem. |
| `server_host` | empty | Address the X3 uses to reach the app: `192.168.1.50` or `192.168.1.50:8765`. Empty: the Home Assistant host's LAN address is detected. |
| `retention_days` | `30` | Items older than this are deleted from Home Assistant (1 to 3650). Files already on the X3 stay. |
| `fetch_timeout_seconds` | `20` | How long to wait for a shared web page (5 to 120). |
| `pdf_enabled` | `true` | Extract the text of shared and emailed PDFs. |
| `pdf_ocr` | `false` | OCR scanned PDFs that have no text layer (Tesseract, English; roughly 10-20 s per page on a Pi 4, max 20 pages). Off: they arrive with a note. |
| `allow_private_urls` | `false` | Allow fetching pages on private, loopback and link-local addresses (NAS, router). Off as a safety measure. Off also means pages are fetched over IPv4 only: IPv6 addresses and IPv6-only sites are refused. |

### Tokens

| Option | Default | What it does |
|---|---|---|
| `ingest_token` | generated | Sent by shortcuts and scripts to the share API: `Authorization: Bearer <token>`, or `?token=<token>`. |
| `device_token` | generated | Used by the X3: the plugin sends it as a bearer token, OPDS uses it as the password for user `x3`. Printable ASCII only: no spaces, `{`, `}`, `"` or `\`. After changing it, reinstall the plugin and update the OPDS server on the X3. To replace a generated one, see below. |
| `admin_token` | generated | Opens the web UI on the LAN port: `http://<IP>:8765/admin?token=<token>`. Not needed inside Home Assistant. |

**The device token can be read from the X3.** The plugin keeps it in `device.json`
in its folder on the X3's SD card (`/.crosspoint/plugins/sendtox3/`). While File
Transfer is open on the X3, any device on your Wi-Fi can read that file from the
X3's web server, and the token opens the plugin catalog and the OPDS feed
(everything in your queue). If it may have leaked, replace it: open **Setup** in
the web UI and select **New device token**.

- If you enter the X3's address while it shows File Transfer > Join a Network, the
  plugin is reinstalled with the new token right away. Otherwise install the plugin
  again (over Wi-Fi or with the SD-card bundle): the old token stops working at once.
- If the X3 uses the OPDS feed (CrossPoint 1.6.5), also enter the new token as its
  OPDS password.
- The button is only there for a generated token. A `device_token` you set in the
  options is replaced by changing it there and restarting the app; then reinstall
  the plugin and update the OPDS password.

### Email (IMAP)

The app checks a mailbox itself (outgoing connection only). A dedicated Gmail label
with a filter works well. Each message can become several items: the text, links,
PDF attachments and `.txt`/`.html` attachments.

Which mail is used (the app fails closed):

- With `imap_enabled` on, mail is processed only when `allowed_senders` and/or
  `imap_plus_tag` is set. With neither, the app does not read the mailbox at all,
  and the email status in the web UI says why.
- `allowed_senders: ['*']` (any sender) is accepted only together with
  `imap_plus_tag`, or with a dedicated `imap_folder` other than `INBOX` (a folder or
  label that only your shares go to). In `INBOX` without a tag it would turn all your
  mail into items, so the app refuses it the same way.
- Mail that is refused (wrong sender or address, failed DMARC check, too big,
  nothing readable) is left unread where it is.
- When the app watches `INBOX` without a tag, its first check only looks at unread
  mail since yesterday, so older unread mail is not imported.
- A message that is only a link (up to ten links) plus at most 30 words (300
  characters) of your own is a link share: each page is fetched, and your words are
  kept as a note in the item. Longer messages are saved as text.

| Option | Default | What it does |
|---|---|---|
| `imap_enabled` | `false` | Turn email ingest on. Also set `allowed_senders` and/or `imap_plus_tag`, or no mail is processed. |
| `imap_host` | `imap.gmail.com` | IMAP server (TLS). |
| `imap_port` | `993` | IMAP port. |
| `imap_user` | empty | Usually your full email address. |
| `imap_password` | empty | For Gmail, an app password (your normal password does not work). |
| `imap_folder` | `INBOX` | Folder or Gmail label to watch. A label that only your shares go to is safest. |
| `imap_processed_folder` | empty | Folder or label to move handled mail to. Empty: only mark it read. |
| `imap_plus_tag` | empty | Accept only mail sent to `you+<tag>@...`. Pick something hard to guess. Empty: no tag check, so `allowed_senders` must be set. |
| `allowed_senders` | empty list | Accept only mail from these addresses. An entry without a user name (`example.com` or `@example.com`) accepts a whole domain. `'*'` accepts any sender, but only together with `imap_plus_tag` or an `imap_folder` other than `INBOX`. Empty: only `imap_plus_tag` decides; with neither set, no mail is processed. |
| `require_dmarc` | `true` | Accept only mail your provider marked `dmarc=pass` or aligned `dkim=pass`, so forged senders are rejected. |

### Home Assistant share sheet

| Option | Default | What it does |
|---|---|---|
| `ha_share_enabled` | `true` | Receive pages and text shared to the Home Assistant Companion app (`mobile_app.share` events). |

### X3 delivery

| Option | Default | What it does |
|---|---|---|
| `auto_delivery` | `false` | Off: you fetch new items with the first row of Home > Plugins > Send to X3 (**Get 3 new**). On: the CrossPoint 2.x plugin also downloads new items automatically, joining Wi-Fi at every sleep, on builds newer than 2.0.0rc (with develop commit facaa87); on 2.0.0rc they arrive the next time the X3 uses Wi-Fi. Reinstall the plugin after changing it. |
| `delivery_format` | `epub` | `epub` or `txt` for plugin catalog downloads and LAN push. EPUB reads better and keeps titles. Sleep delivery always uses EPUB. Reinstall the plugin after changing it. |
| `inbox_folder` | `/Inbox` | Folder on the X3's SD card where the plugin saves downloads. |
| `device_host` | empty | X3 IP address for LAN push. Empty: LAN push is off. |
| `push_poll_seconds` | `20` | How often to look for the X3's File Transfer server (5 to 3600). |
| `push_folder` | `/Inbox` | Folder on the X3 that LAN push uploads to. |

## Permissions this app asks for

- **Home Assistant API**: to receive Companion app shares (`mobile_app.share`).
- **Supervisor API (default role)**: to read the host's network address for the
  URLs given to the X3.
- **Port 8765** on the host: the X3 cannot sign in to Home Assistant, so it needs a
  plain LAN port.

## Data and backups

Items, rendered files and generated tokens are stored in the app's data folder and
included in Home Assistant backups. Temporary files are kept in memory.

The app stops while Home Assistant makes a backup and starts again right after
(a cold backup), so the database is copied in a consistent state. Mail sent in the
meantime waits on the mail server, and the X3 picks up new items at its next
delivery. Phone shares sent while the app is stopped fail; send them again.

## Troubleshooting

- Check the **Log** tab. Set `log_level` to `debug` for details.
- `http://<IP>:8765/healthz` answers when the app runs. Turn on **Watchdog** to have
  Home Assistant restart the app if it stops answering.
- The X3 says it cannot connect: use the IP address, not `homeassistant.local`, and
  check that the X3 is on the same network.
