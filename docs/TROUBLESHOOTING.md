# Troubleshooting Send to X3

Example addresses: the Pi is `192.168.1.50`, the X3 is `192.168.1.60`. Labels in
**bold** are what the X3, Home Assistant or the app's web UI show.

## Three quick checks first

1. **Is the app running and reachable?** On your phone, on the home Wi-Fi, open
   `http://192.168.1.50:8765/healthz`. It should show `{"ok":true,"version":"..."}`.
   - It does not load: the app is stopped, the Pi's address changed, or the port was
     changed or turned off in the app's **Network** section. See
     [The web UI cannot be reached](#the-web-ui-cannot-be-reached).
   - It loads on the phone but the X3 still fails: the problem is on the X3's side (its
     network, or an old address in the plugin or OPDS entry).
2. **What does the app think?** Open the web UI. The **Channels** box shows each way in
   and out with **ok**, **waiting**, **warning**, **error** or **disabled** and a short
   reason. Warnings at the top of the Queue page point at specific problems.
3. **What does the log say?** **Settings > Apps > Send to X3 > Log**. See
   [Logs, and how to share them](#logs-and-how-to-share-them).

## The X3 cannot reach the Pi

These show up on the X3 as **Failed to fetch feed** (catalog or OPDS), **Download
failed**, or a long wait followed by an error.

| Symptom | Likely cause | Fix |
|---|---|---|
| `/healthz` fails on the phone too | App stopped or still starting | App page, **Info** tab: **Start**; check the **Log** tab for `listening on`. |
| `/healthz` fails, the Pi has a new address | No DHCP reservation, or the router was reset | Reserve the address ([Setup, Part 1](SETUP.md#part-1-give-the-pi-and-the-x3-fixed-addresses)). Then reinstall the plugin, update the OPDS URL and your shortcuts. |
| `/healthz` works on the phone, not from the X3 | The X3 is on a guest network or another network that blocks devices from each other | Join the X3 to the same network as the Pi: **File Transfer > Join a Network**. |
| The X3 does not see your Wi-Fi at all | 5 GHz-only network; the X3 only uses 2.4 GHz | Turn on 2.4 GHz on the router (or a 2.4 GHz network with its own name). |
| It worked, then stopped after you changed the address, port or token | The plugin (or OPDS entry) still has the old values | Reinstall the plugin ([Setup, Part 5a step 3](SETUP.md#step-3-install-the-plugin-from-the-setup-page)), or edit the OPDS server. |
| You typed `homeassistant.local` somewhere for the X3 | The X3 usually cannot look up `.local` names | Use the IP address. The Setup page shows the right one. |
| The Setup page shows a Docker-internal address (`172.x.x.x`) or "could not be detected" | The app could not ask Home Assistant for the address | Set **Server address for the X3** (`server_host`) to `192.168.1.50`, **Save**, **Restart**, reinstall the plugin. |

## Installing the plugin

The log under **Install plugin on my X3 now** ends with one of these:

| Last line | Cause | Fix |
|---|---|---|
| `Error: could not reach the X3 at ... On the X3 open File Transfer > Join a Network, keep that screen open, and try again.` | The X3 is not on its **File Transfer > Join a Network** screen, or the address is wrong | Open that screen and type exactly the address it shows (without `http://` and the final `/`). **Create Hotspot** does not work for this: the X3 is then on its own network. |
| `Error: CrossPoint 1.6.5 has no plugin support. Use the OPDS feed instead (see Setup), or update the X3 to CrossPoint 2.0.` | The X3 runs 1.6.5 | Use OPDS and/or LAN push ([Setup, Part 5b](SETUP.md#part-5b-x3-with-crosspoint-165-opds-and-lan-push)), or update the firmware. |
| `Error: The server address is unknown; set server_host in the app options.` | See the last row of the table above | Set `server_host`. |
| `Error: The server_host option is not a usable address; ...` | **Server address for the X3** has a typo, a user name, an empty or impossible port | Correct it to the Pi's address, optionally with a port (`192.168.1.50` or `192.168.1.50:8765`), **Save**, **Restart**. |
| `Error: http://172.... is an address inside Docker that the X3 and your phone cannot reach; ...` | Home Assistant did not report the Pi's address when the app started (the app keeps retrying in the background) | Wait a minute and reload the Setup page, or set `server_host` to the Pi's address. |
| `Warning: could not create /Inbox; ...` | The SD card is full or read-only | Free some space; create an `Inbox` folder at the top of the card. Deliveries at sleep fail without it. |
| `Done: the plugin is installed, but Plugins is off on the X3. ...` | Plugins switched off | **Settings > Connectivity > Plugins > Enabled**. |
| The button is greyed out, with **The plugin cannot be installed yet: ...** | The server address is unknown or unusable | Do what the message says (usually: set `server_host`). The SD-card bundle and the Android file are refused for the same reason. |
| The Setup page warns **The device token from the app options cannot be used by the X3 plugin** | A **Device token** you set contains a space, `{`, `}`, `"`, `\` or a non-ASCII character | Choose one with letters and digits, **Save**, **Restart**, then install the plugin. |

## On the X3 (CrossPoint 2.x plugin)

| What you see | Cause | Fix |
|---|---|---|
| No **Plugins** row on Home | Plugins are off (the default) | **Settings > Connectivity > Plugins > Enabled**. 1.6.5 has no plugins at all. |
| **Plugins** has no **Send to X3** | The plugin files are not on the card, or not in the right place (for example the zip was extracted into a subfolder) | Reinstall over Wi-Fi from the Setup page, or extract the zip at the top level of the card (Mac: with `unzip` in Terminal, because Finder hides `.crosspoint`). |
| **Web-only plugin** or **Plugin manifest is missing or invalid** | `device.json` on the card is damaged | Reinstall the plugin. |
| **Not signed in. Open the web Settings page and sign in via the plugin first.** | The app refused the device token: the token was changed after the plugin was installed | Reinstall the plugin. (There is nothing to sign in to; this is the X3's standard message for a refused token.) |
| **Nothing new right now** as the first row | Everything you shared already reached the X3 (any way), or nothing was shared yet | Share something, then open **Send to X3** again. Single items are still listed below it. |
| Pressing **Confirm** on **Nothing new right now** shows **Download failed** (also **Open** on the Send to X3 firmware) | Normal: there is nothing to fetch | Go back. |
| The button still says **Fetch** on the Send to X3 firmware, and books do not open | The plugin was installed by Send to X3 0.2.0 or older, or **Settings > System > About** does not show `2.0.0-sx3+...` | Reinstall the plugin ([Setup, Part 5a step 3](SETUP.md#step-3-install-the-plugin-from-the-setup-page)); its log should say `Confirm opens the book: ...`. If it says `Confirm downloads; ...`, the X3 runs other firmware: see [The Send to X3 firmware](FIRMWARE.md). |
| **Open** opens an older copy of a book instead of downloading it | Send to X3 firmware: a file with the same name is already in `/Inbox`, and it opens that one (where you left off) | Delete the file in **Browse Files** to download it again. |
| **Open** on an item you already have from **Get N new** downloads it again and starts at page 1 | Send to X3 firmware: a **Get N new** book is saved under that row's name, and the item's own row only finds a file named after the item | Normal: open the **Get N new** book from Home (recent books) or the **Library**, where it keeps your page. Delete the extra copy in **Browse Files** if you do not need it. |
| **Open** shows **Cannot open file**, or **Download complete**, instead of opening the book | Send to X3 firmware: the X3 could not note which book to open, because writing to the SD card failed (for example, the card is full) | Open the book from the **Library**. Free space on the card; if it keeps happening, check the card in a computer. |
| The catalog ends with an empty page (only **Previous Page**) | A plugin installed by 0.2.0 on a newer CrossPoint build (develop of 2026-10-27 or later, or the Send to X3 firmware) | Reinstall the plugin. Every row is still listed once. |
| **Get 3 new** shows **Download failed** | The list was loaded again elsewhere (the OPDS feed, or page 1 once more) with different new items, so that row is out of date; or you pressed it a second time after it counted as delivered (the file is already in **Inbox**); or one of the general causes below | Go back to Home and open **Send to X3** again. |
| The same items come again as **Get 3 new · ... (2)** right after you fetched them | The download was cancelled or failed before the X3 had all of it, so nothing was saved: offered again on purpose, under a new name. Or a plugin installed by 0.1.0, which cannot report saved files | Fetch it again. If it keeps coming back although the book is in **Inbox**, reinstall the plugin and see the app's **Log** tab. |
| **Get 3 new · ... (2)** is offered after you already have **Get 3 new · ...** | The X3 did not report the first file within two hours, so the same items come again under a new name (a second copy; the first is never replaced) | Delete the copy you do not need in **Browse Files**. Frequent cases: you never went back to Home after fetching, you reinstalled the plugin from the SD-card bundle before the X3 reported the fetch (the bundle empties its queue of reports), or other plugins queue many events (the X3 sends at most 4 per Wi-Fi join). |
| The web UI says **sent ..., not confirmed yet**, and the first row says "Your last fetch (...) is confirmed the next time the X3 joins Wi-Fi" | Normal right after a fetch | Go back to Home (the X3 restarts), then open **Home > Plugins > Send to X3** again. Opening it again from the plugin list keeps Wi-Fi on and reports nothing. |
| **New reading could not be prepared** as the first row | The app could not build the download, for example because the Home Assistant data disk is full | The items below still download one by one. See the app's **Log** tab; free space on the Home Assistant disk. |
| You expected an item to arrive by itself | Since version 0.2.0 nothing arrives by itself: you fetch with **Get 3 new** | Open **Home > Plugins > Send to X3** and press **Confirm** on the first row, or turn on delivery at sleep ([Setup, Part 5a step 7](SETUP.md#step-7-optional-automatic-delivery-at-sleep)). |
| The X3 still shows **Loading...** at every sleep after updating to 0.2.0 | The plugin on the X3 was installed by 0.1.0 with delivery at sleep on | Reinstall the plugin. If the app's **Configuration** was saved under 0.1.0, it may still have **Also deliver when the X3 sleeps** on: turn it off, **Save**, **Restart** first. |
| **Failed to fetch feed** | The X3 cannot reach the Pi, or the app had an error answering (rare) | See [The X3 cannot reach the Pi](#the-x3-cannot-reach-the-pi); check the app's **Log** tab. |
| **Download failed** | The item was deleted in the meantime, the token changed, the X3 is low on memory, or the SD card is full | Go back and open the catalog again; restart the X3 (hold power, then wake it); check free space on the card; reinstall the plugin if the token changed. |
| The X3 restarts when you leave the catalog | Normal: leaving a network screen with Wi-Fi on restarts the X3 to Home | Nothing to fix. |
| A file you fetched again starts from page 1 | Normal: fetching replaces the file and the X3 forgets its position | Avoid fetching items you are reading. (On the Send to X3 firmware, **Open** again on an item's row opens the copy that row saved, where you left off. Reopen a **Get N new** book from Home or the **Library**.) |

## Installing the Send to X3 firmware

Details: [The Send to X3 firmware](FIRMWARE.md).

| What you see | Cause | Fix |
|---|---|---|
| The Send to X3 file is not in the **SD Card Firmware Update** list (or it shows **No .bin files found**) | The file is inside a folder, its name does not end in `.bin`, or it starts with a dot | Open the folder in the list, or move the file to the top level of the card. Give it a name like `crosspoint-2.0.0-sx3+<code>-x3-x4.bin`. |
| **Invalid firmware file**, **Firmware file is too small** or **Firmware too large for partition** | The file is damaged or incomplete (an interrupted copy or upload), or not firmware at all | Copy it again. Nothing on the X3 was changed. |
| **Firmware is for a different device** | The file is not an X3/X4 build | Use the file from `firmware/dist`, or the official file ending in `-x3-x4.bin`. |
| **Firmware write failed** | The X3 could not write the update | Try again; if it repeats, use the web flasher ([FIRMWARE.md](FIRMWARE.md#if-that-does-not-help-the-web-flasher-usb-cable)). |
| The X3 does not start normally after an update | The new firmware fails early | Recovery mode: unplug the USB cable, hold the side **Up** button while pressing **Power**, then pick the official file in **Select firmware file (.bin)** ([FIRMWARE.md](FIRMWARE.md#if-the-x3-does-not-start-recovery-mode)). If that shows **SD card error** or nothing, use the web flasher. |
| **About** still shows the old version | The update was not confirmed, or it was cancelled | Repeat **Settings > System > SD Card Firmware Update** and press **Confirm** at **Update firmware?**. |
| `firmware/build.sh` stops with an error about Docker | Docker Desktop is not running | Start Docker Desktop, wait until it says it is running, and run the command again. |
| An LCP-protected book no longer opens | Self-built firmware has no LCP key | Read it on the official firmware ([going back](FIRMWARE.md#going-back-to-the-official-firmware)). |
| **Check for Updates** offers an official release | A release newer than 2.0.0 is out | Installing it replaces the Send to X3 firmware (the button says **Fetch** again). Skip it if you want to keep opening books from the plugin. |

## Nothing arrives when the X3 goes to sleep

Only relevant if you turned on the optional delivery at sleep; by default nothing
arrives by itself (use **Get 3 new**).

| Cause | How to tell | Fix |
|---|---|---|
| **CrossPoint 2.0.0rc** | The version starts with `2.0.0` and the build is the 2026-10-19 prerelease | Known firmware bug: after a wake, 2.0.0rc only delivers once Wi-Fi has been used. Open **Home > Plugins > Send to X3** before sleeping, or use a build with the fix (develop `facaa87`, 2026-10-23, or later). See [Setup, Part 5a step 7](SETUP.md#step-7-optional-automatic-delivery-at-sleep). |
| Nothing new | Every item already shows something in **On the X3** (for example `catalog`) | Delivery at sleep only sends items that never reached the X3. Use **Requeue** to send one again. |
| **Also deliver when the X3 sleeps** is off (the default) | **Channels** shows: "The X3 still requests sleep delivery although auto_delivery is off: reinstall the plugin to stop it (it also joins Wi-Fi at each sleep)." | Turn it on and reinstall the plugin, or reinstall the plugin to stop the requests for good. |
| Plugin installed while delivery was off | No **Loading...** popup at sleep; the plugin's description in **Home > Plugins** starts with "Receives: downloads" instead of "Receives: sleep and current book" | Turn the option on, **Save**, **Restart**, reinstall the plugin. |
| Battery below 20 % | | Charge the X3. |
| Wi-Fi password not saved, or the last network used is not in range | **File Transfer > Join a Network** shows the list of networks instead of connecting by itself | Pick your home network, enter the password, answer **Yes** to **Save password for next time?** The X3 uses the network it last connected to. |
| The X3 cannot save the file | The Queue page warns "The X3 asked N times for the delivery of ...", and **Recent automatic deliveries** shows **requested N×** | Make sure `/Inbox` exists on the SD card and the card has free space. |
| The X3 cannot reach the Pi | **Channels > X3 plugin (CrossPoint 2.x)** shows no recent request | See [The X3 cannot reach the Pi](#the-x3-cannot-reach-the-pi). |
| Another plugin uses up the deliveries | You have several plugins that react to sleep | The X3 runs at most 4 plugin deliveries each time it delivers; remove plugins you do not use. |

## OPDS (CrossPoint 1.6.5, also 2.x)

| What you see | Cause | Fix |
|---|---|---|
| No **OPDS Browser** on Home | No OPDS server saved (1.6.5), or plugins are on (2.x: it is inside **Home > Plugins**) | Add the server ([Setup, Part 5b.2](SETUP.md#5b2-add-the-opds-server)). |
| **Failed to fetch feed** | Wrong address, wrong password, or the X3 cannot reach the Pi | The URL must be exactly `http://192.168.1.50:8765/opds`; user name `x3`; password the **Device token** (copy it again; re-enter it after the token changed). Then see [The X3 cannot reach the Pi](#the-x3-cannot-reach-the-pi). |
| **No entries found** | The URL points at the wrong page (for example without `/opds`), or nothing is in the queue | Correct the URL; share something. |
| **Failed to parse feed** | The URL points at something that is not an OPDS feed | Use the address from the Setup page. |
| **Download failed** | The X3 is low on memory, or the item was deleted | Restart the X3 and try again. |
| A new download replaced an older file | Two items with the same site and title (same file name) | The X3 overwrites files with the same name; this is how its OPDS browser works. |

## LAN push does not happen

| Cause | How to tell | Fix |
|---|---|---|
| `device_host` is empty | **Channels > LAN push to the X3** is **disabled**: "Set device_host in the options to push files to the X3." | Set **X3 address for LAN push**, **Save**, **Restart**. |
| The X3 is not on the right screen | **LAN push to the X3** stays **waiting** | On the X3 open **File Transfer > Join a Network** (or **Calibre Wireless**) and keep it open for half a minute. Not **Create Hotspot**. |
| Wrong or changed X3 address | The address on the X3's File Transfer screen differs from `device_host` | Reserve the X3's address ([Setup, Part 1.2](SETUP.md#12-the-x3)) and correct `device_host`. |
| Nothing new | All items show something in **On the X3** | Push only sends items that never reached the X3. Use **Requeue**. |
| The X3 refused a file | **LAN push to the X3** shows **warning** with the reason | Usually a full SD card. Free space; the app retries the next time the X3 appears. |
| Files arrived but are not in the Library | You are still on the File Transfer screen | Press **« Exit**; the Library updates when you open it. Or look in **Browse Files > Inbox**. |

## Email does not arrive

First look at **Channels > Email (IMAP)** and at the log (search for `IMAP`).

| Symptom | Cause | Fix |
|---|---|---|
| **disabled**: "Turn on imap_enabled in the options to receive email." | Email is off | Turn on **Receive by email** and fill in the IMAP options ([Setup, Part 6](SETUP.md#part-6-email-optional-works-from-anywhere)). |
| **error**: "set imap_user, imap_password to enable email ingest" (or similar) | A required IMAP option is empty | Fill it in, **Save**, **Restart**. |
| **error**: "allowed_senders and imap_plus_tag are both empty, so no mail is accepted: ..." | Safety rule: with email on, the app does not read the mailbox unless **Allowed senders** and/or **Secret plus-address tag** is set | Set both, **Save**, **Restart**. Mail sent meanwhile is still unread and is picked up then. |
| **error**: "allowed_senders is '*' (any sender) but the app watches INBOX without imap_plus_tag, ..." | `*` in **Allowed senders** would turn all mail in your inbox into items | Set a **Secret plus-address tag**, or a **Mailbox folder** (Gmail label) other than `INBOX` that only your shares go to, or list your own addresses instead of `*`. |
| **ok**, with "Watching INBOX without imap_plus_tag: mail there from allowed senders becomes items and is marked read ..." | A warning, not an error: the app reads your inbox | Fine if the mailbox is only for Send to X3. Otherwise set a plus tag or a dedicated label. |
| An error about logging in | Wrong app password, the Google account password was changed (that cancels app passwords), or 2-Step Verification was turned off | Create a new app password and enter it in **IMAP password** (without spaces). |
| **ok**, but your mail is not in the queue, and it is still unread in Gmail | The mail was not accepted; the app leaves such mail untouched | The log has a line `IMAP: message <n> rejected: ...` with the reason, see below. Mail that does not look meant for the app (not sent to the plus address, or from another sender in your inbox) is only logged with **Log level** `debug`. |
| **ok**, the mail was marked read, but no item appeared | The app failed on that message three times (a crash or an error while reading it) and gave up | The log has `giving up on message ...` or `failed (attempt 3/3)` with the error. Send the content another way (a link, or an upload in the web UI). |
| **ok**, but the mail is not in the inbox at all | Gmail put it in Spam | Create the filter with **Never send it to Spam** ([Setup, Part 6.4](SETUP.md#64-keep-that-mail-out-of-spam)); move the mail back to the inbox. |
| You opened the mail on your phone before the app's very first check, or it is older than a day | On its first check the app only reads unread mail (in `INBOX` without a plus tag, only unread mail since yesterday) | Mark the mail as unread, or send it again. After the first check, new mail is read even if you opened it. |

Reasons in the log (`rejected: ...`) and what to do:

| Reason contains | Fix |
|---|---|
| `not addressed to the secret +tag address` (log level `debug`) | Send to `<name>+<tag>@gmail.com`, not to the plain address. Check the tag in the options. |
| `is not in allowed_senders` | Add the address you send from to **Allowed senders** (or send from a listed address). |
| `DMARC`, `aligned dkim`, `Authentication-Results` | The sender could not be verified. Normal mail from Gmail, iCloud or Outlook passes; mail passed on by forwarding services or mailing lists may not. Send directly from your own address. Turn off **Require authenticated senders** only if you understand the risk ([Security](SECURITY.md#email-acceptance-rules)). |
| `MB (cap 25 MB)` | The mail is too large. Send big PDFs as a link, or upload them in the web UI (up to 30 MB). |
| `has no readable content` | The mail had no text, link, PDF, TXT or HTML attachment (for example only photos). |

## Home Assistant Companion app shares do not arrive

Look at **Channels > Home Assistant share sheet**.

| What it shows | Cause | Fix |
|---|---|---|
| **ok**: "Listening for shares from the Home Assistant Companion app" | It works; the share did not reach Home Assistant, or was made while Send to X3 was stopped or restarting (Home Assistant does not keep such shares) | Share again. In the Companion app, check that it is signed in to your Home Assistant. |
| **disabled**: "Turned off (ha_share_enabled)." | **Home Assistant share sheet** is off | Turn it on, **Save**, **Restart**. |
| **warning** or **error** with "retrying in ... s" | The app lost its connection to Home Assistant, or Home Assistant refused its token | It retries by itself. If it stays, restart the app; check the log for `HA share`. |
| **error**: "Could not save a share, retrying in 60 s: ..." | The disk is full or the database failed | Free space on the Home Assistant data disk; the share is saved once that works. |

## Phone shortcut errors

The phone guides have full tables: [iPhone](phone-ios.md) and [Android](phone-android.md).
The most common ones:

| Message | Fix |
|---|---|
| **Send to X3: not saved** with "invalid or missing ingest token" | Copy the **Ingest token** again from the Setup page into the shortcut. |
| "Not saved: the server could not store it (...). Try again later." | The Pi's disk is full or the database failed. Nothing was saved. Free space on the Home Assistant data disk (the Queue page shows a warning too), then share again. |
| "Could not connect to the server", "The request timed out", "Could not reach the Send to X3 app" | You are not on the home Wi-Fi, the Pi's address changed, or the app is stopped. Check `/healthz` from the phone. |
| "A server with the specified hostname could not be found" | Use the IP address instead of a `.local` name. |
| "...still processing. It will appear on the X3 shortly." | Not an error: a large page or PDF is still being converted. It appears in the queue shortly. |
| "Already in the queue: <title>" | Not an error: the same link was shared in the last 24 hours. |
| "Saved, but could not extract the text: ..." | The page could not be read (paywall, login, bot check). The item still has the title and link. |
| "...This link points to a local or private network address, which is blocked ..." or "This server only has an IPv6 address; ..." | The fetching guard refused the link (see [Security](SECURITY.md#fetching-links-safely)). Turn on **Allow local network URLs** only if you really want such pages. |

## PDF problems

| What you see | Cause | Fix |
|---|---|---|
| An item that says "Scanned PDF: no text layer" | The PDF contains only page images | Turn on **OCR scanned PDFs** (`pdf_ocr`), **Save**, **Restart**, send it again. Slow on a Pi. |
| "This PDF is password-protected" | The PDF needs a password to open | Remove the password on your computer and send it again. |
| "PDF import is turned off in the app options (pdf_enabled)" | **Convert PDFs** is off | Turn it on. |
| The phone says "still processing" | Long PDFs take time on a Pi (up to about 2 minutes, 10 with OCR) | Wait; the item appears in the queue. |
| "Converting this PDF took longer than ... seconds, so it was stopped." | The PDF is very long or slow to read (often OCR of a long scan) | Send a shorter part, or turn off **OCR scanned PDFs** for this one. |
| Tables, formulas or forms look jumbled | Known limits of turning a PDF into plain text | See [PDFs: limits](pdf.md#limits). |

## The web UI cannot be reached

| Symptom | Cause | Fix |
|---|---|---|
| No **Send to X3** in the sidebar | **Show in sidebar** is off | App page, **Info** tab: turn on **Show in sidebar**. |
| **Open Web UI** shows an error or a blank page | The app is stopped or still starting, or it failed to start | **Info** tab: **Start**; read the **Log** tab. |
| `http://192.168.1.50:8765/admin` asks you to **Sign in** | Outside Home Assistant the web UI needs the admin token | Enter the **Admin token** (from the Setup page, opened through Home Assistant), or open `/admin?token=<admin token>` once. |
| "That is not the admin token." | Wrong token | Copy it again from the Setup page. |
| **Form expired**: "This form is no longer valid (security check failed). Reload the page and try again." | The page was open too long, or you signed out in another tab | Reload the page. |
| Nothing answers on port 8765 at all | The port is empty (turned off) in the app's **Network** section; the log says "the app's network port 8765/tcp is disabled in Home Assistant" | Set it to `8765`, **Save**, **Restart**. |
| The app does not appear in the app store (local install) | The folder is in the wrong place or incomplete | It must be `local_apps/send_to_x3/config.yaml`. Select **Check for updates** again; read **Settings > System > Logs** with **Supervisor** selected. |
| A field you need is missing from the Configuration form | Home Assistant hides optional fields that are not set | Turn on **Show unused optional configuration options** at the bottom of the form. Note: this switch name is Home Assistant's; it was not checked for this guide. |

## Logs, and how to share them

1. **Settings > Apps > Send to X3 > Configuration**: set **Log level** to `debug`,
   **Save**, then **Restart** on the **Info** tab.
2. Repeat what went wrong (share again, open the catalog, send the email).
3. Open the **Log** tab and copy the lines from the time of the problem. With the
   Terminal & SSH app you can also run `ha apps logs local_send_to_x3` (for Option B
   installs the name starts with the repository's ID instead of `local`).
4. Set **Log level** back to `info` afterwards.

What the log contains: the app never writes tokens or the IMAP password into the log.
It does contain the links you shared, item titles, email addresses of senders and IP
addresses on your network. Read through the lines before you post them and replace
anything private.

If the app does not start at all, look at **Settings > System > Logs** with
**Supervisor** selected (the menu at the top right), and search for `send_to_x3`.

For problems on the X3 itself, a photo of the screen with the exact message helps.
