# Set up Send to X3, step by step

This guide takes you from nothing to "share something on the phone, read it on the X3".
Plan about an hour. You do each part once.

| Part | What you do | Needed for |
|---|---|---|
| [1](#part-1-give-the-pi-and-the-x3-fixed-addresses) | Give the Pi and the X3 fixed addresses in your router | Everything |
| [2](#part-2-check-and-maybe-update-the-x3-firmware) | Check the X3 firmware, update it if you want the plugin (or the Send to X3 firmware, to read right from it) | Choosing Part 5a or 5b |
| [3](#part-3-install-the-send-to-x3-app-in-home-assistant) | Install the Send to X3 app in Home Assistant | Everything |
| [4](#part-4-configure-the-app-and-check-the-setup-page) | Check the options and the app's Setup page | Everything |
| [5a](#part-5a-x3-with-crosspoint-2x-the-plugin) | X3 on CrossPoint 2.x: install the plugin | 2.x |
| [5b](#part-5b-x3-with-crosspoint-165-opds-and-lan-push) | X3 on CrossPoint 1.6.5: OPDS and/or LAN push | 1.6.5 (also works on 2.x) |
| [6](#part-6-email-optional-works-from-anywhere) | A dedicated Gmail inbox for sending from anywhere | Optional |
| [7](#part-7-phones) | Phone shortcuts and the Home Assistant share sheet | Sending from a phone |
| [8](#part-8-final-check) | End-to-end checklist | Peace of mind |

**Conventions in this guide**

- Example addresses: the Raspberry Pi is `192.168.1.50`, the X3 is `192.168.1.60`.
  Use your own everywhere you see these.
- `Settings > Apps` means: select **Settings**, then **Apps**.
- `<device token>` and similar mean "the real value, copied from the app's Setup page".
  Never post these values anywhere.
- On the X3 you move with the buttons and press the confirm button to select. The
  screen's own labels are written in **bold** exactly as the X3 shows them.

## What you need

- A **Raspberry Pi 4 or 5 with Home Assistant OS (64-bit)** and an administrator
  login. If your Calibre apps already run on it, it is 64-bit: they need it.
  A wired (Ethernet) connection for the Pi is best.
- An **Xteink X3** with its microSD card inserted.
- **Home Wi-Fi with a 2.4 GHz network.** The X3's chip (ESP32-C3) only does 2.4 GHz.
  Most routers broadcast 2.4 and 5 GHz under one name, which is fine.
- **Access to your router's admin page** (for Part 1).
- A **phone** (iPhone/iPad or Android) on the same Wi-Fi, and a **computer** with a web
  browser. To update the X3 firmware with the web flasher you need Chrome or Edge and a
  USB-C data cable (installing from the SD card needs neither; see Part 2.3).
- A **Mac** only if you want the ready-made iPhone shortcut (otherwise you build it by
  hand in 10 minutes, see Part 7).
- Optional: a **new Gmail account** for sending by email (Part 6).

---

## Part 1. Give the Pi and the X3 fixed addresses

**Why:** the X3 plugin, the OPDS server entry on the X3 and the phone shortcuts all
contain the Pi's address. LAN push contains the X3's address. If your router hands out
a different address later, everything stops working until you update it. A **DHCP
reservation** (some routers call it "address reservation", "static lease" or "fixed
IP") tells the router to always give a device the same address.

Note: every router brand names and places this setting differently; no particular
router was tested for this guide. Look in its manual for "DHCP reservation".

### 1.1 The Raspberry Pi

1. Find the Pi's current address. In Home Assistant open **Settings > System >
   Network**; the IPv4 address of the network adapter is shown there (for example
   `192.168.1.50`). Your router's list of connected devices also shows it, usually as
   `homeassistant`.
   Note: the exact place of the address on the Network page was not checked.
2. In your router's admin page, create a DHCP reservation for the Pi with **the address
   it already has**. Then nothing changes right now.
3. **Check:** on your phone (on Wi-Fi), open `http://192.168.1.50:8123` in the browser.
   The Home Assistant login page appears.

### 1.2 The X3

1. On the X3 open **Settings > System > About** and note the **MAC Address** row
   (on 1.6.5: **Settings**, then the **System** tab, then **About**).
2. In the router, create a DHCP reservation for that MAC address, for example
   `192.168.1.60`. Once the X3 has connected, the router lists it as
   `CrossPoint-Reader-` followed by the MAC address without colons.
3. The reservation takes effect the next time the X3 connects to Wi-Fi. You will check
   it in Part 5a or 5b, where the X3 shows its address.

Make sure the X3 and the Pi are on the **same network**. A guest network usually blocks
traffic between devices, so the X3 must not join it.

---

## Part 2. Check (and maybe update) the X3 firmware

### 2.1 Which version do you have?

On the X3 open **Settings**. The firmware version is shown at the top right of the
screen. It is also the **Firmware** row in **Settings > System > About**.

| You see | What it means | Continue with |
|---|---|---|
| `1.6.5` | Stable release. No plugins. | Part 5b (OPDS and/or LAN push). Updating is optional. |
| Starting with `2.0.0` | CrossPoint 2.x with plugins. | Part 5a. |
| `2.0.0-sx3+` and a code | The Send to X3 firmware (2.x with plugins, see 2.2). | Part 5a. |
| Anything older than 1.6.5 | Not tested with Send to X3. | Update (2.3). |

Note: a 2.0.0rc build may show extra characters after `2.0.0` (for example an `-rc`
suffix); that is still 2.x.

### 2.2 Which firmware should you run?

| Firmware | What you get with Send to X3 |
|---|---|
| **1.6.5** (stable) | No plugins. You fetch items with the X3's **OPDS Browser**, and/or the app copies new items onto the X3 whenever its **File Transfer** screen is open (LAN push). Perfectly usable. |
| **2.0.0rc** (prerelease, 2026-10-19) | The **Send to X3 plugin**: open it on the X3 and press Confirm on the first row, **Get 3 new**, to download everything you shared since last time as one book, or pick single items from the list below it. Nothing arrives by itself. (Delivery at sleep is an option with a caveat on 2.0.0rc; see [Part 5a step 7](#step-7-optional-automatic-delivery-at-sleep).) |
| **A newer 2.x build** (develop commit `facaa87`, 2026-10-23, or any later release or nightly) | The same plugin. The optional delivery at sleep also works with no taps. |
| **The Send to X3 firmware** (CrossPoint develop of 2026-10-28 plus one change; version `2.0.0-sx3+...`) | The same plugin, and you read right from it: the button on **Get 3 new** says **Open**, and pressing it downloads the book and opens it in the reader. **Open** again on a single item's row opens the copy that row saved where you left off. You build it on your Mac and install it from the SD card: see [The Send to X3 firmware](FIRMWARE.md). |

Staying on 1.6.5 is fine. If you do, skip to Part 3. The Send to X3 firmware is
optional too; you can install it at any time later, and go back.

### 2.3 Flash CrossPoint 2.0.0rc or later (high level)

For the Send to X3 firmware follow [its own guide](FIRMWARE.md) instead: it is
installed from the SD card, with no cable. (The SD card route also works for an
official release file: put it on the card and use **Settings > System > SD Card
Firmware Update**; see that guide.)

For an official release, the CrossPoint project's own instructions are the reference:
see "Install firmware" in the
[CrossPoint README](https://github.com/crosspoint-reader/crosspoint-reader#install-firmware).
In short:

1. Charge the X3. As a precaution, copy the whole SD card to your computer, including
   the hidden `.crosspoint` folder with your settings and reading positions (a Mac:
   [The Send to X3 firmware, Before you start](FIRMWARE.md#before-you-start), item 3).
2. Download the X3 firmware file for the version you want from the
   [CrossPoint releases page](https://github.com/crosspoint-reader/crosspoint-reader/releases)
   (2.0.0rc is marked as a pre-release).
3. On the computer, open <https://crosspointreader.com/#flash-tools> in **Chrome or
   Edge**. Connect the X3 with a USB-C data cable and wake it up.
4. Select **X3**. Either pick the release from the list, or choose **Custom .bin** and
   select the file from step 2.
5. Wait until the flasher says it is done. Do not unplug the X3 while it writes.
6. **Check:** the X3 restarts. Open **Settings** and look at the version (2.1).

If the browser does not list the X3, try another cable or USB port first. Some X3s
bought from third-party shops are "USB-locked"; read the CrossPoint README's section on
the unlock tool **before** using it, because flashing the wrong thing on a locked
device can make it unrecoverable.

Note: the flasher was not run for this guide; the steps are summarised from the
CrossPoint README. Whether the flasher's list offers prereleases, and where nightly
builds are published, was not checked. Your books and settings live on the SD card,
which flashing does not normally touch, but keep the backup from step 1 anyway.

---

## Part 3. Install the Send to X3 app in Home Assistant

Home Assistant calls add-ons **apps** since version 2026.2. There are two ways to
install this one. **Use only one**: two copies would both want port 8765 and would
have separate data and tokens.

- **Option A, local app:** you copy the `send_to_x3` folder onto the Pi. Works today.
- **Option B, app repository:** you paste the project's GitHub address into Home
  Assistant (`https://github.com/kimolas/send-to-x3`). Home Assistant then offers
  updates like for any other app.

### Option A: install as a local app

You need to put the `send_to_x3` folder of this repository into the Pi's
**local apps** folder. Pick A1 (Samba, easiest) or A2 (Terminal & SSH).

#### A1. Copy the folder with the Samba share app

1. In Home Assistant open **Settings > Apps > Install app**. Search for **Samba share**,
   select it and select **Install**.
2. Open its **Configuration** tab, set a user name and a password for the share (make
   up new ones; they are only for this share), and select **Save**.
   Note: the field names in the Samba share form were not checked; see its
   **Documentation** tab.
3. Open its **Info** tab and select **Start**.
4. Connect from your computer:
   - **Mac:** in Finder choose **Go > Connect to Server** (Cmd+K), enter
     `smb://192.168.1.50` (or `smb://homeassistant.local`), select **Connect**, choose
     **Registered User** and sign in with the Samba user name and password. Pick the
     share **`local_apps`**.
   - **Windows:** in File Explorer's address bar type `\\192.168.1.50\local_apps` and
     sign in with the Samba user name and password.

   Older Home Assistant versions call this share `addons`.
5. Copy the **`send_to_x3` folder itself** (not only its contents, and do not rename
   it) into `local_apps`. Afterwards `local_apps/send_to_x3/config.yaml` must exist.

   The folder may contain a hidden `.mypy_cache` folder (a developer cache, about 8 MB)
   that the app does not need. To copy without it on a Mac, use Terminal instead of
   Finder (the share is mounted at `/Volumes/local_apps`):

   ```sh
   rsync -a --exclude .mypy_cache --exclude __pycache__ "<path to this repository>/send_to_x3" /Volumes/local_apps/
   ```

#### A2. Or copy the folder with the Terminal & SSH app

1. Install **Terminal & SSH** the same way. In its **Configuration** tab add your SSH
   public key (its **Documentation** tab explains how), in the **Network** section set
   a port (for example `22`), select **Save**, then **Start** on the **Info** tab.
2. On your Mac or Linux computer, run in a terminal (one line):

   ```sh
   tar -C "<path to this repository>" --exclude .mypy_cache --exclude __pycache__ -cf - send_to_x3 | ssh root@192.168.1.50 'tar -C /local_apps -xf -'
   ```

3. **Check:** open the Terminal & SSH app (**Open Web UI**) and run
   `ls /local_apps/send_to_x3`. You see `config.yaml`, `Dockerfile`, `DOCS.md`,
   `run.sh`, `sendtox3` and a few more.

Note: the user name `root` and the folder `/local_apps` come from Home Assistant's
developer documentation; on older systems the folder is `/addons`. Check the Terminal &
SSH app's Documentation tab if the command is refused.

#### A3. Install the app

1. Open **Settings > Apps > Install app** (the app store). In the menu at the top right
   (three dots) select **Check for updates**. Reload the browser page (Cmd+Shift+R on a
   Mac, Ctrl+F5 on Windows).
2. A section **Local apps** appears with **Send to X3** in it. Select it.
3. Select **Install**. Home Assistant now builds the app on the Pi, which takes several
   minutes. Wait until the page shows the **Start** button.

   Note: the build time on a Raspberry Pi was not measured.

If **Send to X3** does not appear: open **Settings > System > Logs**, choose
**Supervisor** in the menu at the top right, and look for lines that mention
`send_to_x3`. They say what is wrong with the folder.

**Updating later (Option A):** copy the new `send_to_x3` folder over the old one, then
**Check for updates** in the app store. If the version number changed, the app page
offers **Update**; otherwise use **Rebuild** on the app page. Your items, options and
tokens are kept.

### Option B: install from the app repository

1. Open **Settings > Apps > Install app**, then the menu at the top right, then
   **Repositories**.
2. Paste the repository address `https://github.com/kimolas/send-to-x3`
   and select **Add**, then close the dialog.
3. The app store shows a section named **Send to X3**. Select the app, then
   **Install**.

**Prebuilt images:** if the published `send_to_x3/config.yaml` contains an `image:`
line, Home Assistant downloads a ready-made image (quick) instead of building it on
your Pi. Updates then show up like any other app update.

### First start

1. On the app's page (**Settings > Apps > Send to X3**), **Info** tab:
   - leave **Start on boot** on,
   - turn on **Watchdog** (Home Assistant restarts the app if it stops answering),
   - turn on **Show in sidebar**,
   - select **Start**.
2. Open the **Log** tab. After a few seconds you see lines like these:

   ```
   Send to X3 0.1.0 listening on 0.0.0.0:8765 (data in /data, 0 items)
   server address for the X3 and phones: http://192.168.1.50:8765 (supervisor)
   ```

   If instead you see a warning such as `could not detect this server's LAN address;
   set server_host in the options` or `... is an address inside Docker that the X3
   cannot reach; set server_host`, carry on: Part 4.2 fixes it.
3. Select **Open Web UI** (or **Send to X3** in the sidebar). The **Queue** page opens
   with three boxes: **Send something**, **Channels** and **Queue**. No token is asked
   for: Home Assistant has already signed you in.
4. **Check from your phone** (on Wi-Fi): open `http://192.168.1.50:8765/healthz`. The
   page shows `{"ok":true,"version":"0.1.0"}` (the version may differ). If the phone
   can open it, the X3 on the same network normally can too.

---

## Part 4. Configure the app and check the Setup page

### 4.1 The options

The options are on the app's **Configuration** tab. The defaults work, so you can leave
everything as it is for now. Whenever you change something: select **Save**, then
**Restart** on the **Info** tab.

| Label in the form (option) | Set it to | Why |
|---|---|---|
| Log level (`log_level`) | `info` | Use `debug` only while you chase a problem. |
| Server address for the X3 (`server_host`) | empty | Only set it (for example `192.168.1.50`) if the Setup page shows a wrong address (4.2). |
| Share token, Device token, Admin token (`ingest_token`, `device_token`, `admin_token`) | empty | Generated on first start and shown on the Setup page. |
| Receive by email (`imap_enabled`) and the IMAP options | off for now | Part 6. |
| Home Assistant share sheet (`ha_share_enabled`) | on | Lets you share from the Home Assistant Companion app. |
| X3 address for LAN push (`device_host`) | empty, or the X3's address (`192.168.1.60`) | Only for LAN push (Part 5b). |
| LAN push check interval (seconds) (`push_poll_seconds`) | `20` | |
| LAN push folder (`push_folder`) | `/Inbox` | |
| Also deliver when the X3 sleeps (`auto_delivery`) | off | Leave it off: you fetch new items with **Get 3 new** on the X3. See Part 5a step 7. |
| Download format (`delivery_format`) | `epub` | EPUB reads better and keeps titles. `txt` gives plain `.txt` files. |
| X3 inbox folder (`inbox_folder`) | `/Inbox` | |
| Keep items for (days) (`retention_days`) | `30` | Older items are removed from Home Assistant; files on the X3 stay. |
| Convert PDFs (`pdf_enabled`) | on | |
| OCR scanned PDFs (`pdf_ocr`) | off | Turn on only if you send scans; slow on a Pi ([PDF guide](pdf.md)). |
| Web page timeout (seconds) (`fetch_timeout_seconds`) | `20` | |
| Allow local network URLs (`allow_private_urls`) | off | A safety guard, see [Security](SECURITY.md#fetching-links-safely). |

The full option reference is the app's **Documentation** tab (the same text as
[send_to_x3/DOCS.md](../send_to_x3/DOCS.md)).

**The port.** There is no port option in the form. The app's port appears in the
**Network** section at the bottom of the Configuration tab, as `8765/tcp` with the
value `8765`. Leave it. If you really must use another port (because another app
already uses 8765), change it there; the app reads the new port from Home Assistant
by itself. If you also set **Server address for the X3**, include the new port
(`192.168.1.50:9000`). Do not empty the field: then nothing can reach the app. After
any address or port change, reinstall the X3 plugin, update the OPDS server on the X3
and update your phone shortcuts.

### 4.2 The Setup page

Open the web UI and select **Setup** at the top.

1. **Server address check.** The first box says, for example: "The X3 and your phone
   reach this app at `http://192.168.1.50:8765` (detected from Home Assistant)".
   - The address must be the Pi's address from Part 1, with `:8765` (or the port you
     set in the **Network** section).
   - If it is different, or the page warns that the address looks like an address
     inside Docker, or says that the address could not be detected or that the
     server_host option is not a usable address: set **Server address for the X3** to
     the Pi's address (`192.168.1.50`), **Save**, **Restart**, and reload the Setup
     page. It now says "(from the server_host option)".
   - **Check:** after the restart the first box shows the Pi's address with `:8765`
     and no warning.
2. **Tokens.** The **Tokens** box shows the **Ingest token** (phones and shortcuts),
   the **Device token** (the X3) and the **Admin token** (the web UI from outside Home
   Assistant), each with a **Copy** button. They are created on first start and kept
   across restarts and updates. Treat them like passwords. Below them is the **New
   device token** button, for when the device token may have leaked (it can be read
   from the X3 while File Transfer is open). How to replace each token is in
   [Security](SECURITY.md#if-a-token-leaks-or-you-want-new-ones).
3. The rest of the page has one box per way of delivering and sending:
   **X3 with CrossPoint 2.x: the Send to X3 plugin**, **X3 with CrossPoint 1.6.5:
   OPDS**, **iPhone and iPad: share sheet shortcut**, **Android, computers and
   scripts** and **Email**. The following parts tell you which ones to use. While the
   server address is unknown or unusable, the plugin and Android downloads are refused
   with the reason; fix the address first (step 1).

---

## Part 5a. X3 with CrossPoint 2.x: the plugin

You end up with **Home > Plugins > Send to X3** on the X3. Its first row, **Get 3 new
· Oct 26 14.05**, downloads everything you shared since the last fetch as one book.
Below it is everything you shared, newest first, to download one at a time. Nothing
arrives on the X3 by itself (unless you turn on delivery at sleep, step 7).

### Step 1. Turn on plugins

On the X3 open **Settings > Connectivity > Plugins** and choose **Enabled**. The Home
screen now has a **Plugins** row.

### Step 2. Save your Wi-Fi and open File Transfer

1. On the X3 open **Home > File Transfer > Join a Network**.
2. Pick your home network and type the password with the on-screen keyboard. (If the
   X3 already knows the network, it connects by itself; go on with item 4.)
3. When asked **Save password for next time?**, choose **Yes**. This matters: the X3
   uses the saved password every time you open the plugin.
4. The screen now says **Open this URL in your browser** and shows an address such as
   `http://192.168.1.60/`. That is the X3's address; it should be the one you reserved
   in Part 1. **Keep this screen open** for step 3.

### Step 3. Install the plugin from the Setup page

1. On the computer, open the app's **Setup** page, box **X3 with CrossPoint 2.x: the
   Send to X3 plugin**, part **Install over Wi-Fi**.
2. Type the X3's address without `http://` (for example `192.168.1.60`) and select
   **Install plugin on my X3 now**.
3. **Check:** after a few seconds the page shows **Plugin installed.** and a log similar
   to this:

   ```
   Connecting to the X3 at 192.168.1.60 ...
   Found X3 running CrossPoint 2.0.0...
   Confirm downloads; install the Send to X3 firmware to open books directly (docs/FIRMWARE.md).
   /Inbox is ready.
   Wrote /.crosspoint/plugins/sendtox3/device.json (... bytes ...).
   Wrote /.crosspoint/plugins/sendtox3/manifest.json (... bytes ...).
   Wrote /.crosspoint/plugins/sendtox3/README.md (... bytes ...).
   Done: the plugin is installed. Leave File Transfer, then open Home > Plugins > Send to X3 ...
   ```

   The third line says what **Confirm** does on this X3. On the
   [Send to X3 firmware](FIRMWARE.md) it reads `Confirm opens the book: this X3's
   firmware opens catalog downloads in the reader.` instead.

   If the last line starts with `Error:`, see
   [Troubleshooting](TROUBLESHOOTING.md#installing-the-plugin).
4. On the X3 press **« Exit**. The X3 restarts to the Home screen; that is normal.

**Alternative: copy the plugin to the SD card.** If the plugin is already on the X3 (a
reinstall), first go back to Home and open **Home > Plugins > Send to X3** once, so the
X3 reports your last fetch: the bundle empties the X3's queue of reports. (The install
over Wi-Fi does not need this.) On the Setup page select **Download SD-card plugin
bundle (zip)** (`sendtox3-sd-card.zip`). Put the X3's SD card into your computer and
extract the zip at the **top level** of the card. It contains a hidden folder
`.crosspoint/plugins/sendtox3` and an `Inbox` folder. A Mac's Finder hides folders
that start with a dot, so use Terminal:

```sh
unzip -o ~/Downloads/sendtox3-sd-card.zip -d "/Volumes/<name of the card>"
```

Eject the card and put it back into the X3. The bundle contains the server address and
the device token, so download it again if either changes.

### Step 4. Open the plugin

1. On the X3 open **Home > Plugins**. You see **Send to X3** with the description
   "Receives: downloads. Articles and notes you shared." (The X3 tells the app which
   downloads it saved; with delivery at sleep on it says "Receives: sleep and current
   book." instead.) A **Plugin Hub** row may also be there; you do not need it.
2. Select **Send to X3**. The X3 connects to Wi-Fi and loads the list. Before you have
   shared anything it has one row, **Nothing new right now**. That is expected.

### Step 5. Test with a pasted note

1. On the computer, in the web UI's **Queue** page, box **Send something**: paste a
   paragraph of any text into **Text or a link**, type `Test note` as the **Title** and
   select **Send to X3**.
2. **Check:** the page shows **Sent to X3: Test note**, and the item appears in the
   **Queue** table with **waiting** in the **On the X3** column.
3. On the X3 open **Home > Plugins > Send to X3**. The first row is **Get 1 new · Oct
   26 14.05** (the date and time of your newest share), with a line below it like
   `Everything new in one book · 120 words: Test note`. Under it is **Test note** itself
   (`New · 120 words · note`).
4. The first row is already selected: press **Confirm** (the button says **Fetch**). The
   X3 shows **Downloading...**, then **Download complete**.
5. Go back to Home (the X3 may restart; that is normal) and open **Library**: **Test
   note** is there. Open it and read.
6. **Check in the web UI:** the item's **On the X3** column shows **sent ..., not
   confirmed yet**. The X3 confirms the saved file the next time it joins Wi-Fi: go back
   to Home (the X3 restarts) and open **Home > Plugins > Send to X3** again. The web UI
   now shows `fetch` with the time, and the first row reads **Nothing new right now**.
   (Had the download been cancelled, it would be offered again, as **Get 1 new · ...
   (2)**.)

**On the [Send to X3 firmware](FIRMWARE.md)** steps 4 to 6 are shorter: the button
says **Open**. Pressing it shows **Downloading...**, then a short **Loading** message;
the X3 restarts straight into **Test note**. The web UI already shows `fetch`: the X3
reported the file before it opened it. Pressing **Open** again on a row whose book is
already on the X3 (the same file in `/Inbox`) opens it where you left off, without
downloading it again. To go back to a **Get N new** book, open it from Home or the
**Library**: **Open** on one of its items' own rows downloads a separate copy of that
item, which starts at page 1.

**Every day from now on:** share from your phone; when you want to read it, on the X3
open **Home > Plugins > Send to X3**, press **Confirm** on the first row, then go back
(on the Send to X3 firmware the book opens by itself).
Several new items arrive as one book with a chapter per item (one new item keeps its
own title). To get a single item again, select it in the list below the first row (it
then shows `catalog`).

### Step 6. Where the files go

| How it arrived | File on the SD card | Title in the Library |
|---|---|---|
| You selected **Get 3 new** | `/Inbox/Get 3 new · Oct 26 14.05.epub` (a name is never reused: a second bundle in the same minute ends in ` (2)`) | One item: its title. Several: `Inbox: 3 items (2026-10-26 14:05)`. (With Download format `txt`: a `.txt` file, shown by its file name.) |
| You selected a single item | `/Inbox/<title>.epub` (`.txt` with Download format `txt`) | The item's title |
| Delivery at sleep (optional, step 7) | `/Inbox/<numbers>.epub`, for example `/Inbox/1792929600-1a2b3c4d-1.epub` | One item: its title. Several: `Inbox: 3 items (2026-10-25 21:40)` |

You find them in **Library**, or in **Browse Files > Inbox**.

### Step 7. Optional: automatic delivery at sleep

Skip this unless you want items to arrive with no taps at all. With **Also deliver
when the X3 sleeps** on, every time the X3 goes to sleep on your home Wi-Fi it also
fetches everything you shared that has not reached it yet, as one EPUB. **Get 3 new**
keeps working either way.

To turn it on: in Home Assistant open the app's **Configuration** tab, turn on **Also
deliver when the X3 sleeps**, select **Save**, then **Restart** on the **Info** tab, and
reinstall the plugin (step 3). Then test it:

1. In the web UI, send another note, titled `Sleep test`.
2. Put the X3 to sleep: hold the power button (or let it fall asleep on its own). On
   2.0.0rc, first open **Home > Plugins > Send to X3** and hold the power button while
   the catalog is on screen (see the caveat below).
3. You see a short **Loading...** popup, then **Send to X3: new reading delivered**,
   then **Going to sleep**.
4. **Check:** wake the X3; **Library** has **Sleep test**. In the web UI the item shows
   `auto` in **On the X3**, and **Channels > X3 plugin (CrossPoint 2.x)** mentions the
   sleep delivery. The **Recent automatic deliveries** list shows it too.

It only works when the battery is at 20 % or more, the Wi-Fi password was saved (step
2) and the last network the X3 used is in range.

**The 2.0.0rc caveat.** On 2.0.0rc the X3 cannot load the saved Wi-Fi password at
sleep right after waking up, and every wake is a restart. So a normal "wake, read,
sleep" does **not** deliver: the X3 only notes that it wants a delivery. The delivery
then happens the next time Wi-Fi is used: when you open **Home > Plugins > Send to X3**
(or the OPDS Browser, File Transfer or KOReader sync), or when you put the X3 to sleep
while Wi-Fi is already on. A simple routine on 2.0.0rc: open **Home > Plugins > Send to
X3** (this also delivers anything waiting), then put the X3 to sleep from there. The
bug is fixed in CrossPoint develop commit `facaa87` (2026-10-23); with a newer build
or nightly the delivery at sleep needs no taps at all.

**The cost.** With automatic delivery on, the X3 joins Wi-Fi at every sleep. That uses
some battery and adds up to about 10 seconds before it sleeps (longer away from home,
when the Pi cannot be reached). To go back to fetching items yourself, turn off **Also
deliver when the X3 sleeps**, **Save**, **Restart**, and reinstall the plugin (step 3).
Until you reinstall, the X3 keeps asking at every sleep; the app answers "nothing new"
and the **Channels** box shows a warning.

**If you set up version 0.1.0:** reinstall the plugin after updating (step 3). The old
plugin has delivery at sleep built in and cannot report saved files. If you saved the
app's **Configuration** under 0.1.0, it may still have **Also deliver when the X3
sleeps** on (it was the default then): turn it off, **Save** and **Restart** first.

**If you set up version 0.2.0:** reinstall the plugin after updating (step 3). The
0.2.0 plugin cannot open books on the Send to X3 firmware, and on newer CrossPoint
builds its catalog can end with an empty page. Before reinstalling from the SD-card
bundle, go back to Home and open **Home > Plugins > Send to X3** once, so the X3
reports your last fetch; the bundle empties the X3's queue of reports. The install
over Wi-Fi from the Setup page does not need this.

**Reinstall the plugin (step 3) after changing** the server address or port, the
device token, **Also deliver when the X3 sleeps**, **Download format** or **X3 inbox
folder**. (**New device token** with the X3's address in its box reinstalls it for
you.)

---

## Part 5b. X3 with CrossPoint 1.6.5: OPDS and LAN push

CrossPoint 1.6.5 has no plugins. You have two ways, and you can use both:

- **OPDS:** the X3's **OPDS Browser** lists everything you shared; you pick what to
  download. Always EPUB.
- **LAN push:** whenever the X3's **File Transfer** screen is open, the app copies all
  new items onto it.

Both also work on CrossPoint 2.x.

### 5b.1 Save your Wi-Fi

1. On the X3 open **Home > File Transfer > Join a Network**, pick your home network and
   type the password.
2. When asked **Save password for next time?**, choose **Yes**.
3. The screen says **Open this URL in your browser** and shows the X3's address, for
   example `http://192.168.1.60/`. Keep this screen open for 5b.2.

### 5b.2 Add the OPDS server

The device token is long, so the easiest way is the X3's own web page, where you can
paste it:

1. With the File Transfer screen still open, open `http://192.168.1.60/settings` in the
   computer's browser.
2. In the **OPDS Servers** box select **+ Add Server** and fill in:

   | Field | Value |
   |---|---|
   | **Server Name** | `Send to X3` |
   | **URL** | `http://192.168.1.50:8765/opds` (the Setup page shows it under **X3 with CrossPoint 1.6.5: OPDS**, with a **Copy** button) |
   | **Username** | `x3` |
   | **Password** | the **Device token** from the Setup page |

   Then select **Save**.
3. On the X3 press **« Exit**. It restarts to Home, which now has an **OPDS Browser**
   row.

**Or on the X3 itself:** **Settings > System > OPDS Servers > Add Server**, then fill in
**Server Name**, **OPDS Server URL**, **Username** and **Password** with the on-screen
keyboard (same values as above).

**Optional:** in **Settings > System > OPDS Servers** set **Download Folder** to
`/Inbox` (the default is **SD root**), so downloads are kept together.

**Test:**

1. In the web UI, send a note titled `OPDS test` (as in Part 5a step 5).
2. On the X3 open **Home > OPDS Browser** (choose **Send to X3** if you have several
   servers). The first entry is **Get 1 new · <date time>** (everything new as one
   book); below it are your items, newest first.
3. Select **OPDS test** itself. A progress bar runs; the file is saved in the download
   folder as `<site> - <title>.epub` (here `note - OPDS test.epub`), and the list
   appears again.
4. **Check:** **Library** has **OPDS test**. In the web UI the item shows `opds` in
   **On the X3**. From now on, selecting the first entry gets everything new at once
   (`fetch` in the web UI; OPDS cannot confirm saved files, so it counts once sent).

On CrossPoint 2.x the OPDS servers are under **Settings > Connectivity > OPDS
Servers**, and with plugins on, the **OPDS Browser** is listed inside **Home > Plugins**.

### 5b.3 Turn on LAN push

1. In the app's **Configuration** tab set **X3 address for LAN push** (`device_host`)
   to the X3's address, `192.168.1.60`. **Save**, then **Restart**.
2. **Check:** in the web UI, **Channels > LAN push to the X3** is **waiting** (the X3 is
   not in File Transfer mode yet).
3. Send a note titled `Push test`.
4. On the X3 open **Home > File Transfer > Join a Network** and leave the screen open
   for about half a minute (the app looks for the X3 every 20 seconds). The X3 itself
   shows nothing while files arrive.
5. **Check:** in the web UI the item shows `push` in **On the X3**, and **Channels >
   LAN push to the X3** is **ok**.
6. Press **« Exit** on the X3. Back on Home, open **Library**: it updates itself and
   shows **Push test**. The file is `/Inbox/Push test.epub` (`.txt` with Download
   format `txt`). If a file with that name already exists, the new one is called
   `Push test (2).epub`.

While File Transfer is open the X3 does not fall asleep by itself and uses more
battery, so press **« Exit** when you are done. **Calibre Wireless** in the File
Transfer menu starts the same server, so LAN push works there too.

---

## Part 6. Email (optional, works from anywhere)

With email you can send links, text, forwarded newsletters and PDFs from any device,
also away from home. The app checks a mailbox itself (an outgoing connection from the
Pi; nothing is opened to the internet).

Use a **new Gmail account only for this**. The app password you create gives full
access to that mailbox, so keep your personal mail out of it.

### 6.1 Create the account and turn on 2-Step Verification

1. Create a new Google account at <https://accounts.google.com/signup>, for example
   `jane.x3inbox@gmail.com`.
2. Signed in as that account, open <https://myaccount.google.com/signinoptions/twosv>
   and turn on **2-Step Verification** (Google walks you through it; a phone number is
   enough). App passwords need it.

### 6.2 Create an app password

1. Open <https://myaccount.google.com/apppasswords> (signed in as the new account).
2. Enter an app name, `Send to X3`, and select **Create**.
3. Google shows a 16-character password once. Copy it; you type it into the app in 6.5
   (without the spaces).

Good to know: app passwords do not work with work or school accounts, or when the
account only allows security keys (Advanced Protection). Changing the account's
password cancels all app passwords; then create a new one. Gmail always allows IMAP,
so there is nothing to switch on in Gmail's settings.

Note: Google's page and button names may differ slightly from these.

### 6.3 Choose a secret plus tag

Gmail delivers mail for `jane.x3inbox+anything@gmail.com` to `jane.x3inbox@gmail.com`.
The app only accepts mail sent to your secret "plus" address, so random mail to the
plain address is ignored.

Make up a tag that is hard to guess, for example `x3-` followed by six random letters
and digits. Do not copy the example `x3-7f2k` from this guide. Your sending address is
then `jane.x3inbox+<your tag>@gmail.com`.

### 6.4 Keep that mail out of Spam

1. Open Gmail on the web, signed in as the new account.
2. In the search box select **Show search options** (the sliders icon at the right).
3. In **To** enter `jane.x3inbox+<your tag>@gmail.com` and select **Create filter**.
4. Tick **Never send it to Spam** and select **Create filter**.

Note: Gmail's labels as of 2026; they may change.

### 6.5 Set the email options

In the app's **Configuration** tab:

| Label in the form (option) | Value |
|---|---|
| Receive by email (`imap_enabled`) | on |
| IMAP server (`imap_host`) | `imap.gmail.com` |
| IMAP port (`imap_port`) | `993` |
| IMAP user name (`imap_user`) | `jane.x3inbox@gmail.com` (the plain address, without the tag) |
| IMAP password (`imap_password`) | the app password from 6.2 |
| Mailbox folder (`imap_folder`) | `INBOX` |
| Move processed mail to (`imap_processed_folder`) | empty (handled mail is only marked as read) |
| Secret plus-address tag (`imap_plus_tag`) | your tag, without the `+` |
| Allowed senders (`allowed_senders`) | the address(es) **you send from**, one per entry, for example `jane@example.com`. An entry without a user name (`example.com` or `@example.com`) allows a whole domain. `*` allows anyone, but only together with a plus tag or a **Mailbox folder** other than `INBOX`. |
| Require authenticated senders (`require_dmarc`) | on |

If you prefer typing YAML, the menu at the top right of the form has **Edit in YAML**;
the email part then looks like this (with your values):

```yaml
imap_enabled: true
imap_host: imap.gmail.com
imap_port: 993
imap_user: jane.x3inbox@gmail.com
imap_password: "<app password>"
imap_folder: INBOX
imap_processed_folder: ""
imap_plus_tag: "<your tag>"
allowed_senders:
  - jane@example.com
require_dmarc: true
```

Note: the **Edit in YAML** menu entry is Home Assistant's; its exact place was not
checked.

**Save**, then **Restart**.

**Safety rule:** with **Receive by email** on, the app only reads mail when **Allowed
senders** and/or the **Secret plus-address tag** are set. If both are empty, it does
not even open the mailbox, and on the Queue page **Channels > Email (IMAP)** shows
**error**: "allowed_senders and imap_plus_tag are both empty, so no mail is accepted:
...". The same happens with `*` as the only allowed sender while **Mailbox folder** is
`INBOX` and no tag is set. Set both a tag and your own addresses.

### 6.6 Check and test

1. **Check:** the Setup page's **Email** box says "Send or forward mail to
   `jane.x3inbox+<your tag>@gmail.com`" and lists your allowed senders. In the Queue
   page, **Channels > Email (IMAP)** is **ok**.
2. **Test a link:** from an allowed address, send an email to the plus address with
   the subject `Test` and a web link as the only text, for example
   `https://en.wikipedia.org/wiki/Electronic_paper`. Within about a minute the article
   appears in the **Queue** (the small line under the title reads `url · email · ...
   words`).
3. **Test a PDF:** send another email with a PDF attached. It appears as its own item
   (kind `pdf`). Long PDFs take a while; see the [PDF guide](pdf.md).
4. **Test a comment:** send a link with a short line of your own above it, for example
   "read before Friday". The item is the article, and your words are at its top as
   `Note: Comment: read before Friday`.

How mail is treated:

- On its very first check the app reads the unread messages in the folder (in `INBOX`
  without a plus tag: only unread mail since yesterday); after that, every new
  message, even if you already opened it on your phone.
- A message with 1 to 10 links and at most 30 words of your own becomes the
  article(s), your words kept as a comment. Longer text becomes a note. Each PDF, TXT
  or HTML attachment becomes its own item.
- Mail that is not accepted (wrong address, sender not allowed, failed sender check,
  too big, nothing readable) is **left untouched** in the mailbox, still unread. The
  app's log says why (look for `rejected:`; set **Log level** to `debug` to also see
  mail that was not meant for the app). Google's own welcome mails stay unread for
  this reason; that is expected.
- Accepted mail is marked as read (or moved, if you set **Move processed mail to**; the
  Gmail label must exist first).
- A message the app fails on three times (even across restarts) is marked read and
  skipped, so it cannot block the rest; the log says so.

---

## Part 7. Phones

Each phone guide has every step and its own troubleshooting table.

### iPhone and iPad: [docs/phone-ios.md](phone-ios.md)

| Way | Setup |
|---|---|
| **Send to X3 shortcut** (recommended): Share > Send to X3; at home it sends directly and shows "Sent to X3: <title>", away it sends an email | iPhones only open signed shortcut files, and only a Mac signed in to iCloud can sign one, so the app's Setup page cannot offer it as a download. Either sign the ready-made shortcut on a Mac (`python3 shortcuts/build_shortcut.py --sign people-who-know-me`, then AirDrop it to the iPhone) or build it by hand in the Shortcuts app in about 10 minutes. Both are in the iPhone guide. |
| **Home Assistant Companion app**: Share > Home Assistant | Nothing, if the Companion app is installed and signed in |
| **Email**: Share > Mail to your plus address | Part 6 |

### Android: [docs/phone-android.md](phone-android.md)

| Way | Setup |
|---|---|
| **HTTP Shortcuts** (recommended): Share > HTTP Shortcuts; shows "Sent to X3: <title>" | Install the free HTTP Shortcuts app. On the app's **Setup** page, box **Android, computers and scripts**, select **Download HTTP Shortcuts file** (open the Setup page on the phone, or send the file to it). The file already contains the server address and your ingest token, and your plus address if email is set up. In HTTP Shortcuts open the menu > **Import / Export** > **Import from File** and pick the file. Delete the downloaded file afterwards: it contains your token. (The download is refused while the Setup page has no usable server address; fix that first, Part 4.2.) |
| **Home Assistant Companion app**: Share > Home Assistant | Nothing, if the Companion app is installed and signed in |
| **Email**: Share > Gmail to your plus address | Part 6 |

### The Home Assistant Companion app on any phone

**Home Assistant share sheet** (`ha_share_enabled`) is on by default. Share a page or
text, choose **Home Assistant** and send. It works wherever the Companion app reaches
your Home Assistant. You get no confirmation from Send to X3 itself (check the Queue
page), and a share made while the Send to X3 app is stopped is lost, because Home
Assistant does not keep such events.

**Check:** on the Queue page, **Channels > Home Assistant share sheet** is **ok**
("Listening for shares from the Home Assistant Companion app"). Share a web page to
**Home Assistant** from the phone: within a few seconds the article is in the
**Queue**, with `ha_share` under its title.

### Computers

Paste text or a link into **Send something** in the web UI, upload a PDF, TXT, HTML or
EPUB file there, or send an email (Part 6). Scripts can use the share API; the Setup
page's **Android, computers and scripts** box shows a ready `curl` command.

---

## Part 8. Final check

Tick these off once:

- [ ] `http://192.168.1.50:8765/healthz` opens on the phone and shows `"ok":true`.
- [ ] The Setup page shows the Pi's reserved address, with `:8765`.
- [ ] The X3 shows the reserved address on its **File Transfer > Join a Network**
      screen.
- [ ] **2.x:** after sharing something new, **Home > Plugins > Send to X3** starts with
      **Get 1 new**; pressing Confirm puts it in the Library. The web UI shows **sent
      ..., not confirmed yet**, then `fetch` once you went back to Home and opened the
      plugin again. Nothing new arrives without that (unless you turned on step 7).
- [ ] **Send to X3 firmware (if used):** the button on **Get 1 new** says **Open**;
      pressing it opens the book, and the web UI shows `fetch` straight away.
- [ ] **2.x:** selecting a single item in the list below the first row works too.
- [ ] **1.6.5:** **Home > OPDS Browser** lists your items; downloading one works.
- [ ] **LAN push (if used):** opening **File Transfer > Join a Network** for half a
      minute copies new items (`push` in the web UI).
- [ ] **Phone at home:** sharing a web page shows "Sent to X3: <title>" (shortcut) or
      the item appears in the Queue (Companion app).
- [ ] **Email (if used):** **Channels > Email (IMAP)** is **ok**, and a link emailed
      from an allowed address to the plus address appears in the Queue within about a
      minute.
- [ ] **Companion app (if used):** **Channels > Home Assistant share sheet** is **ok**,
      and a shared page appears in the Queue.
- [ ] **Channels** shows no **error** for the ways you use, and the Queue page shows no
      warning at the top.

Good to know: Home Assistant backups include the app's items, options and tokens.
While a backup is made, Home Assistant stops the app for a moment and starts it again
(a "cold" backup); a phone share sent in that moment fails and has to be sent again.

Then read [Using Send to X3](USING.md) for daily use. If something does not work, see
[Troubleshooting](TROUBLESHOOTING.md). For what is exposed on your network and how to
keep it safe, see [Security](SECURITY.md).
