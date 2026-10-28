# The Send to X3 firmware

With the Send to X3 firmware on your X3, you read right from the plugin page: in
**Home > Plugins > Send to X3**, pressing **Open** on **Get 3 new** downloads your new
reading and opens it in the reader. You do not need it: Send to X3 works with the
official CrossPoint firmware too, where the same press saves the book and you open it
from the Library.

This page explains what the Send to X3 firmware is, how to put it on the X3 and how to
go back. It is written for a Mac. You need no programming knowledge, but you do need
the firmware file (a `.bin` file); [Building the firmware](#building-the-firmware)
explains how to make it.

Note: these steps were worked out from the CrossPoint source code, and opening books
was tried in the X3 simulator; neither was yet carried out on a real X3 for this page.
The places where that matters are marked.

## What it is

- **CrossPoint** is the firmware (the software inside the reader) that your X3 runs.
  Its developers publish releases (1.6.5 is the current stable one, 2.0.0rc a
  prerelease) and work on newer code in between, called **develop**.
- The **Send to X3 firmware** is CrossPoint develop as of 2026-10-28 (commit
  `8f4d035`) plus **one change**: a plugin can ask that a book it downloads opens
  in the reader. Nothing else is different.
- develop is newer than 2.0.0rc. It already contains the fixes made since then, for
  example the one that lets the optional delivery at sleep work without taps (commit
  `facaa87`). It is not an official release, so it has had less testing than one.
- On the X3 its version reads `2.0.0-sx3+<code>`, where `<code>` is seven letters and
  digits. The code names the exact build: the same CrossPoint commit and the same
  change always give the same code.

## What it changes on the X3

| Where | Official CrossPoint 2.x | Send to X3 firmware |
|---|---|---|
| The button on a row in **Home > Plugins > Send to X3** | **Fetch** | **Open** |
| Pressing it on **Get 3 new** or on an item | Downloads the book, shows **Download complete**; you open it from the Library | Downloads the book, shows a short **Loading** message while it tells the app the book arrived, then restarts straight into the book (a few seconds) |
| Pressing it again on a row whose book it saved before (the same file in `/Inbox`) | Downloads it again and starts it from page 1 | Opens the copy on the card where you left off; nothing is downloaded |
| When the app learns that the book arrived | The next time the X3 joins Wi-Fi (go back to Home, open **Send to X3** again) | Right away, before the book opens (if the X3 is short of memory at that moment, at the next Wi-Fi join instead) |
| Pressing it on **Nothing new right now** | **Download failed** | **Download failed** (nothing to fetch; nothing is wrong) |

A **Get 3 new** book is saved under that row's own name, and that row is never offered
again. To go back to the book, open it from **Home** (recent books) or the
**Library**. **Open** on one of its items' own rows looks for a file named after the
item, so it downloads that item as a separate book, which starts at page 1.

Only **Open** changes. Books delivered at sleep or by LAN push wait in the Library as
before. Everything else works as in CrossPoint develop: the Library, the reader, OPDS,
File Transfer, other plugins. Other plugins only open books this way if they ask for
it in their own settings.

The Send to X3 plugin must have been installed by Send to X3 **0.3.0 or later**; an
older plugin keeps saying **Fetch**. Install it again from the app's Setup page
([Setup, Part 5a step 3](SETUP.md#step-3-install-the-plugin-from-the-setup-page)): the
log then says `Confirm opens the book: this X3's firmware opens catalog downloads in
the reader.` (on official firmware it says `Confirm downloads; install the Send to X3
firmware to open books directly (docs/FIRMWARE.md).`).

## Before you start

1. **Note the version you have now.** On the X3 open **Settings > System > About** and
   write down the **Firmware** row (on 1.6.5: **Settings**, the **System** tab, then
   **About**). That is the version you would go back to.
2. **Download that official version and keep the file.** On the
   [CrossPoint releases page](https://github.com/crosspoint-reader/crosspoint-reader/releases)
   open your version and download the X3 file. For 2.0.0rc it is
   [`crosspoint-2.0.0rc-x3-x4.bin`](https://github.com/crosspoint-reader/crosspoint-reader/releases/download/2.0.0rc/crosspoint-2.0.0rc-x3-x4.bin)
   (one file serves the X3 and the X4). Keep it in your Downloads folder.
3. **Charge the X3, and back up its SD card.** Your settings, saved Wi-Fi password,
   recent books and reading positions are in a hidden folder on the card,
   `.crosspoint`. Put the card into the Mac (items 1 and 2 of
   [Part B of step 1](#step-1-put-the-file-on-the-sd-card) show how) and copy the
   whole card, hidden folders included, into a new folder in your home folder. In
   Terminal (`<name of the card>` is the name Finder shows for it in the sidebar):

   ```sh
   ditto "/Volumes/<name of the card>" "$HOME/X3-backup-$(date +%F)"
   ls -a "$HOME"/X3-backup-*
   ```

   The list must include `.crosspoint`. (Finder hides that folder: selecting
   everything on the card in Finder leaves it out unless you first press
   **Cmd-Shift-.** to show hidden items.) Eject the card and put it back as in item 3
   of Part B.
4. **Have the Send to X3 firmware file**, named like
   `crosspoint-2.0.0-sx3+<code>-x3-x4.bin`. After a build it is in the `firmware/dist`
   folder of this project.

## Install it

The X3 can update itself from a file on its SD card: **Settings > System > SD Card
Firmware Update**. No cable and no special program are needed. This works the same
way on 1.6.5, 2.0.0rc and develop.

### Step 1. Put the file on the SD card

Put the file at the **top level** of the card (not inside a folder), where the updater
lists it right away. Its name must end in `.bin` and must not start with a dot.
Choose A or B.

**A. Over Wi-Fi (no need to take the card out)**

1. On the X3 open **Home > File Transfer > Join a Network**. The X3 joins your Wi-Fi and
   shows an address such as `http://192.168.1.60/`. Keep that screen open.
2. On the Mac, open that address in Safari or Chrome, then select **File Manager** at
   the top of the page. It shows the top level of the card.
3. Select **Upload**, choose the Send to X3 firmware file and select **Upload** in the
   dialog. Wait until it says the upload is complete (the file is about 5.5 MB).
4. On the X3 press **« Exit**. The X3 restarts to Home; that is normal.

**B. With a card reader**

1. Turn the X3 off or let it sleep, then take out the microSD card and put it into the
   Mac (with a card reader or adapter). It appears in Finder's sidebar.
2. Drag the Send to X3 firmware file onto the card's window, at the top level.
3. In Finder select the eject button next to the card, wait until it disappears, then
   put the card back into the X3.

### Step 2. Update

1. On the X3 open **Settings > System > SD Card Firmware Update**. It lists the card's
   folders and `.bin` files (**Select firmware file (.bin)**).
2. Select the Send to X3 file. The X3 checks it (**Validating firmware...**) and asks
   **Update firmware?** with the file name. Press **Confirm**.
3. A progress bar appears with **Do not power off!**. Leave the X3 alone; it restarts
   by itself when it is done.
4. **Check:** open **Settings > System > About**. **Firmware** reads
   `2.0.0-sx3+...`.

Your books, reading positions, settings and saved Wi-Fi password are on the SD card
and stay as they were. The `.bin` file stays on the card too. To delete it, use the
X3's web **File Manager** (**File Transfer > Join a Network**, open the address,
**File Manager**) or Finder with a card reader; or keep it for later.

If the X3 shows **Invalid firmware file**, **Firmware file is too small**, **Firmware
too large for partition** or **Firmware is for a different device**, the file is
damaged or not the right one. Nothing was changed; copy the file again (step 1).

### Step 3. Install the plugin again and try it

1. Install the plugin from the app's Setup page
   ([Setup, Part 5a step 3](SETUP.md#step-3-install-the-plugin-from-the-setup-page)).
   The log says `Confirm opens the book: ...`. If you came from 1.6.5 (OPDS), the X3
   has not had the plugin yet: start at
   [Part 5a step 1](SETUP.md#step-1-turn-on-plugins) (turn on Plugins), then step 3.
2. Share something, for example a pasted note in the web UI.
3. On the X3 open **Home > Plugins > Send to X3**. The button on the first row now says
   **Open**. Press it on **Get 1 new**: the book downloads and opens.

## If the X3 does not start: recovery mode

If the X3 no longer starts normally after an update, you can start it straight into
the firmware picker and install another file from the card:

1. If the X3 is fully off rather than asleep, unplug the USB cable first. Hold the side
   **Up** button and, while holding it, press the **Power** button to wake the X3.
2. The X3 opens the firmware picker, **Select firmware file (.bin)**, which lists the
   card's folders and `.bin` files. Once you select a file, the screens that follow
   are headed **Recovery Mode**.
3. Select the official firmware file you saved (put it on the card with Part B of step
   1 if it is not there yet) and press **Confirm**. **Back** only shows the list again:
   in recovery mode the X3 waits until you install a file.

Note: the button comes from the firmware source, which names it the side Up button
(the "left side button" on the X3); its exact position was not checked on a real X3.
If one side button does not work, try the other. Recovery mode needs a readable SD
card: if the X3 shows **SD card error**, use the web flasher below. The X3 does not go
back to the previous firmware by itself.

## If that does not help: the web flasher (USB cable)

The CrossPoint project's web flasher writes firmware over a USB cable:

1. Use **Chrome or Edge** on the Mac (Safari cannot talk to USB devices this way) and
   a USB-C **data** cable (some cables only charge).
2. Open <https://crosspointreader.com/#flash-tools>. Connect the X3 and wake it up.
3. Select **X3**. To install the Send to X3 firmware choose **Custom .bin** and select
   the file; to go back, pick an official release from the list (or **Custom .bin**
   with the official file you saved).
4. Wait until the flasher says it is done. Do not unplug the X3 while it writes.

Note: the flasher was not run for this guide; the steps are summarised from the
CrossPoint README. X3s bought from third-party shops can be "USB-locked"; read the
CrossPoint README's section on that **before** using any unlock tool.

## Going back to the official firmware

Do the same as when installing, with the official file you saved in
[Before you start](#before-you-start):

1. Put `crosspoint-2.0.0rc-x3-x4.bin` (or whichever official version you had) at the
   top level of the card ([step 1](#step-1-put-the-file-on-the-sd-card)).
2. **Settings > System > SD Card Firmware Update**, select it, **Confirm**.
3. **Check:** **Settings > System > About** shows the official version.

On CrossPoint 2.x the plugin keeps working and you do not need to install it again:
the button says **Fetch** again and downloads as before. 1.6.5 has no plugins, so use
the OPDS feed or LAN push instead
([Setup, Part 5b](SETUP.md#part-5b-x3-with-crosspoint-165-opds-and-lan-push)).

Settings that are new or were renamed since your version go back to their defaults,
for example **Line Spacing**, or an image or sleep-screen option that only develop
has. Set them again after going back.

Prefer going back to the version you had before. Going from develop back to 1.6.5 is
a step back over a whole release; whether 1.6.5 reads every setting that develop
saved was not checked. If the X3 misbehaves afterwards, restore the `.crosspoint`
folder from the backup you made in [Before you start](#before-you-start): with the
card in the Mac, in Terminal (use your backup folder's date):

```sh
ditto "$HOME/X3-backup-<date>/.crosspoint" "/Volumes/<name of the card>/.crosspoint"
```

That brings back the settings, Wi-Fi and reading positions of the day of the backup.

## The built-in updater

**Settings > System > Check for Updates** only runs when you select it, and asks
before it installs anything. It only knows the official CrossPoint releases, and
offers one only if its number is higher than the running one. For the Send to X3
firmware (`2.0.0-sx3+...`) that means:

| When | What the updater offers |
|---|---|
| Now (the latest official release is 1.6.5; 2.0.0rc is a prerelease) | Nothing |
| When official 2.0.0 is released | Nothing (the same number) |
| When official 2.0.1 or later is released | That release. Installing it replaces the Send to X3 firmware: the button says **Fetch** again. To keep opening books from the plugin, skip that update until a Send to X3 firmware based on it is built. |

## Building the firmware

You build the firmware file on the Mac with one command. Docker does the work, so
nothing else needs to be installed.

**You need:** Docker Desktop (installed and running, see
[Testing, one-time setup](TESTING.md#one-time-setup-on-a-mac)), Apple's command line
tools (`git`), about 6 GB of free disk space, and the internet for the first build.

1. Open Terminal and go to the project folder:

   ```sh
   cd "$HOME/My Drive/Claude/Projects/xteink"
   ```

2. Build:

   ```sh
   firmware/build.sh
   ```

   It downloads CrossPoint at the pinned commit, applies the Send to X3 change, runs
   CrossPoint's own tests for the plugin code, and builds the firmware. **The first
   build takes 20 to 40 minutes** (it downloads the compiler and builds the parts of
   the firmware that rarely change). Later builds take a few minutes.

3. **What you should see:** the last lines name the result:

   ```
   firmware/dist/crosspoint-2.0.0-sx3+<code>-x3-x4.bin
   firmware/dist/crosspoint-2.0.0-sx3+<code>-x3-x4.size.txt
   ```

   where `<code>` is seven letters and digits. Building again from the same project
   gives the same code. The `.bin` file is the firmware to put on the X3. If the
   command stops with an error instead, nothing was installed anywhere; the messages
   above it say what went wrong (most often: Docker Desktop is not running).

The build keeps its tools and downloads in Docker (the image
`sendtox3-firmware-builder:1`, made from `python:3.13-slim`, and about 5 GB in the
volumes `sendtox3-fw-pio` and `sendtox3-fw-build`) and the CrossPoint source in
`~/.cache/sendtox3-firmware`, so later builds are fast. To free the space:

```sh
docker volume rm sendtox3-fw-pio sendtox3-fw-build
docker image rm sendtox3-firmware-builder:1 python:3.13-slim
rm -rf ~/.cache/sendtox3-firmware
```

Developer notes (the patch, the pinned commit, moving to a newer CrossPoint) are in
[`firmware/README.md`](../firmware/README.md).

## One thing a self-built firmware cannot do: LCP books

Official CrossPoint releases contain a key for **LCP**, a copy protection some
libraries and book shops use. Firmware you build yourself does not have it, so
LCP-protected books do not open on it; everything else works. Books from Send to X3
have no copy protection and are not affected. If you read LCP books on the X3, stay on
the official firmware.

Note: this comes from the firmware source ("self-compiled firmware ... does everything
except LCP"); it was not tried with an LCP book.
