# Send to X3

![Supports aarch64][aarch64-shield] ![Supports amd64][amd64-shield]

Share a web page, some text, an email or a PDF from your phone or computer, and
read it on your Xteink X3 a moment later as clean, image-free text.

- Works with **stock CrossPoint** firmware: 1.6.5 (OPDS and LAN push) and 2.x (an
  SD-card plugin). Nothing arrives by itself: on the X3 open Home > Plugins >
  Send to X3 and select **Get 3 new** to download everything new as one book.
  Delivery at sleep is available as an option.
- Optional: the **Send to X3 firmware** (CrossPoint plus one change) opens each book
  in the reader as soon as you select it in the plugin. See `docs/FIRMWARE.md` in
  the repository.
- Send from the **Home Assistant Companion app** share sheet, an **iPhone Shortcut**
  or Android HTTP shortcut, **email** (the app checks your mailbox), or the web UI.
- Articles are extracted without ads, menus or images and turned into small EPUB
  (or TXT) files with proper titles.
- Everything stays on your home network. No cloud service and no port forwarding.

Runs on 64-bit Home Assistant OS: aarch64 (Raspberry Pi 3, 4 and 5) or amd64.

See the **Documentation** tab after installing for setup and every option.

## Installing

1. In Home Assistant open **Settings > Apps > Install app**, open the menu at the top
   right, choose **Repositories**, and add the URL of this Git repository.
2. Find **Send to X3** in the app store, select **Install**, then **Start**.
3. Select **Open Web UI** and follow the setup steps for your X3.

By default the image is built on your Home Assistant host, which takes a few
minutes on a Raspberry Pi. Maintainers who publish prebuilt images (see
`.github/workflows/build.yml`) set `image:` in `config.yaml`.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
