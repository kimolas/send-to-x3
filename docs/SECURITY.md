# Security of Send to X3

Send to X3 is built for a home network you trust. Nothing needs to be reachable from
the internet. This page says what is exposed, who could do what, and how to keep it
tight. Example address of the Pi: `192.168.1.50`.

## In short

- One **plain-HTTP port, 8765**, on your home network. Everything on it except a health
  check needs a token. **Never forward this port on your router.**
- The web UI inside Home Assistant goes through Home Assistant's own login (ingress).
- The X3 cannot use passwords on its side: **while its File Transfer screen is open,
  any device on your Wi-Fi can read the X3's files**, including the device token in the
  plugin.
- Email is **fail-closed**: the app only reads mail sent to your secret plus address
  and/or from your allowed senders, checked against the mail provider's sender
  verification. With neither set it reads no mail at all. Mail it does not accept is
  left untouched.
- Shared links are fetched only from the **public internet**, over IPv4: the app
  refuses to load pages from your own network unless you allow it.
- The data folder (items, database, generated tokens) is readable only by the app's
  own account, and PDFs are converted by a separate, unprivileged process without
  access to it.

## What is exposed

### On the app's port 8765 (plain HTTP, home network only)

| Address | Protected by | Used by |
|---|---|---|
| `/healthz` | Nothing (open) | Home Assistant's watchdog. Answers `{"ok":true,"version":"..."}`; reveals only that the app runs and its version. |
| `/api/v1/ingest` | **Ingest token** (`Authorization: Bearer <token>`, or `?token=<token>`) | Phone shortcuts, scripts |
| `/device/v1/catalog`, `/device/v1/items/...`, `/device/v1/deliver`, `POST /device/v1/fetched` | **Device token** (`Authorization: Bearer <token>`) | The X3 plugin (CrossPoint 2.x) |
| `/opds`, `/opds/items/...` | **Device token** as the HTTP Basic password (any user name; `x3` by convention) | OPDS on the X3 |
| `/admin/...` | **Admin token**, once; then a signed session cookie for 30 days | The web UI from a browser on your network. The Setup page and its downloads (plugin bundle, Android file) contain the device or ingest token. |

The tokens are random 32-character strings, generated on first start (unless you set
your own) and compared in a way that does not leak timing. A wrong admin token on the
sign-in page is answered with a short delay to slow down guessing, and the sign-in
form accepts only a small request (at most 4 KB, read within 10 seconds).

The port is **plain HTTP**: the tokens and the content travel unencrypted over your
home network. Anyone who can watch traffic on that network (a compromised device on
your Wi-Fi, for example) could pick them up. That is the reason for "home network
only". The app does not offer HTTPS on this port; the X3's firmware does not check
server certificates anyway, so HTTPS would not protect the X3's side much.

### Through Home Assistant (ingress)

Opening the web UI from the Home Assistant sidebar or **Open Web UI** needs no token:
Home Assistant has already signed you in, and the app accepts these requests only when
they come from Home Assistant's Supervisor. This also works remotely if you already use
Home Assistant remote access (Home Assistant Cloud or your own setup), without
exposing port 8765.

The web UI protects every form against cross-site requests, sets a strict content
policy, never sends your token on to other sites (no referrer), and keeps tokens out of
its logs.

### On the X3 while File Transfer is open

This is the X3 firmware's design, not something Send to X3 can change:

- While **File Transfer** (**Join a Network**, **Calibre Wireless** or **Create
  Hotspot**) is open, the X3 runs a web server **without any password**. Anyone on the
  same network can list, download, upload and delete files on its SD card.
- The plugin's `device.json` on the SD card contains the server address and the
  **device token** in plain text, and the X3's web server hands out plugin files
  without asking. So **while File Transfer is open, any device on your Wi-Fi can read
  the device token.** Anyone holding the SD card can read it too.
- When the X3 leaves File Transfer (or goes to sleep), the server is gone.

What the device token allows: reading the catalog and the OPDS feed (the titles and full
text of everything in your queue), and fetching **Get N new**, confirming it or the
delivery at sleep (which marks items as delivered, so they would no longer be in your
next **Get N new**). The X3 reports the SD path of every catalog download it saves, from
any plugin, to `POST /device/v1/fetched`; the app ignores reports from other plugins
without reading them and keeps nothing of them. It does
**not** allow adding items, changing settings, seeing the other tokens or using the
web UI.

### Email

The app logs in to the mailbox you configure (outgoing connection from the Pi, TLS on
port 993) and reads new mail. Nothing listens for mail on the Pi.

### The Home Assistant share sheet

Shares from the Home Assistant Companion app arrive as `mobile_app.share` events in
Home Assistant. Anyone who can use your Home Assistant with the Companion app can add
items this way. Turn off **Home Assistant share sheet** (`ha_share_enabled`) if you do
not want that.

### What the app may do in Home Assistant

The app asks Home Assistant for two permissions (shown on its Info tab):

- **Home Assistant API** (`homeassistant_api`): used only to listen for
  `mobile_app.share` events. This access is broad (administrator level), as for every
  app that uses it.
- **Supervisor API, default role** (`hassio_api`): used only to read the Pi's network
  address for the X3 plugin.

## Who could do what (threat model)

| Who | What they could do | What stops or limits it |
|---|---|---|
| Someone on the internet | Nothing directly | No port is opened to the internet. Do not forward 8765. |
| Someone who learns your plus address | Send mail to it | Also needs to pass **Allowed senders** and the sender verification. Rejected mail is ignored. |
| Someone who forges your address as sender | Pretend to be you | **Require authenticated senders** (DMARC or aligned DKIM, from your mail provider's topmost verification header) rejects forged senders. |
| A guest or device on your Wi-Fi, without tokens | Read the X3's files (and the device token) while File Transfer is open; see that the app runs | Keep File Transfer sessions short; put guests on a guest network. |
| Someone with the **ingest token** | Add items to your queue (they would appear on your X3); make the Pi fetch public web pages | Treat it like a password; replace it if it leaks. Fetching is limited to the public internet. |
| Someone with the **device token** | Read everything in your queue; mark items as delivered | Replace it (one button, see below). |
| Someone with the **admin token** | Everything the web UI can do: read and delete items, see all tokens, install the plugin on your X3 | Keep it private; replace it if it leaks. |
| A malicious web page, PDF or email | Try to break the converter, or take it over | Only text is kept (no scripts, images or links in the EPUB). PDFs are converted in a separate process with time, memory and page limits, without the app's tokens in its environment, and (in the Home Assistant app) as an unprivileged account that cannot read the data folder. Uploaded EPUBs are checked for zip bombs and read with size and time limits; all sizes are capped. |
| A link that points into your network (router, NAS, Home Assistant) | Try to have the app read a private page and deliver it to the X3 | The fetching guard below. |

## Email acceptance rules

With **Receive by email** (`imap_enabled`) on, every new message goes through these
checks, in order. A message must pass all checks that are configured:

1. **Size:** at most 25 MB.
2. **Secret plus address** (`imap_plus_tag`): the message must be addressed to
   `<name>+<tag>@...`.
3. **Allowed senders** (`allowed_senders`): the From address must be listed (an entry
   without a user name, such as `example.com`, allows a whole domain; `*` allows
   anyone, see below). With an empty list, the plus address alone decides.
4. **Sender verification** (`require_dmarc`, on by default): the **topmost**
   `Authentication-Results` header, the one your provider (Gmail: `mx.google.com`)
   added, must show `dmarc=pass` or a `dkim=pass` that matches the From domain. Lower
   headers are ignored, because a sender could write those themselves.
5. **Content:** it must contain something readable (text, a link, a PDF, TXT or HTML
   attachment). At most 50 items are taken from one message.

**Fail-closed:** if email is on but **neither** the plus tag nor allowed senders are
set, the app does not even connect to the mailbox, and **Channels > Email (IMAP)**
shows **error**: "allowed_senders and imap_plus_tag are both empty, so no mail is
accepted: ...". The same happens with `*` (any sender) in **Allowed senders** while
the app watches `INBOX` without a plus tag, because every message there that passes
the sender check would become an item; `*` works only together with a plus tag or a
**Mailbox folder** other than `INBOX` that only your shares go to.

**Mail that is not accepted is left untouched** (it stays unread, in place), so a
shared or busy mailbox is not changed by the app. Accepted mail is marked as read, or
moved if you set **Move processed mail to**. When the app watches `INBOX` without a
plus tag, its very first check only looks at unread mail since yesterday, so an old
unread backlog is not imported. A message that makes the app fail three times (even
across restarts) is given up on and marked read, so it cannot block the mailbox.

Recommendations:

- Use a **dedicated** mailbox (a new Gmail account) and an **app password**. The app
  password gives full access to that mailbox, so it should not be your personal one.
- Set **both** a hard-to-guess plus tag **and** your own addresses as allowed senders,
  and leave **Require authenticated senders** on. Without it, the From address is easy
  to fake.
- If the app password may have leaked, delete it at
  <https://myaccount.google.com/apppasswords> and create a new one.

## Fetching links safely

When you share a link, the Pi downloads the page. The fetcher:

- only uses `http` and `https`;
- refuses private, loopback, link-local, multicast and other non-public addresses
  (your router, NAS, Home Assistant, the X3), also when they are written in unusual
  ways (full-width digits, IPv4 inside IPv6), and checks this **after** looking up the
  name and again on every redirect (at most 5), so a site cannot redirect or
  "re-resolve" its way into your network;
- connects over **IPv4 only**: a device on your home network can have a public IPv6
  address that no check can tell apart from a web server, so IPv6 addresses are
  refused, and a site that has only an IPv6 address cannot be fetched;
- ignores proxy settings that could bypass this check;
- limits the size (8 MB for a page, 25 MB for a PDF) and the time of each download
  (**Web page timeout (seconds)**).

**Allow local network URLs** (`allow_private_urls`) turns the address checks off
(including the IPv4-only rule). Only turn it on if you really want to send pages from
a server on your own network, and keep in mind that anyone with your ingest token (or
an accepted email) could then have pages from your network delivered.

The sites you share see a request from your home IP address, as if you had opened the
page yourself. Tracking parameters are removed from shared links.

## Data and backups

- Items, rendered files and generated tokens are stored in the app's data folder on
  the Pi and included in Home Assistant backups. The app keeps that folder readable
  only by itself (folders 0700, files 0600). The options, including the IMAP app
  password, are part of Home Assistant's configuration and backups too. Keep your
  backups protected.
- Backups are "cold": Home Assistant stops the app for the moment it copies the data
  folder, so the database is copied in a consistent state. Shares sent from a phone
  during that moment fail (send them again); mail waits on the mail server.
- Items are deleted after **Keep items for (days)** (default 30). Files on the X3 are
  not touched.
- The log never contains tokens or the IMAP password. It does contain shared links,
  titles, sender addresses and local IP addresses: review it before posting it
  anywhere.

## Recommendations

1. Never forward port 8765 (or any Home Assistant port) on your router for this app.
   To send from away, use email or the Home Assistant Companion app.
2. Put visitors and untrusted devices on a separate guest network.
3. Open the X3's **File Transfer** only when you need it, and press **« Exit** when
   done.
4. Leave the tokens generated (or, if you set your own, use 32 or more random letters
   and digits). Delete the Android import file from the phone once it is imported.
5. For email: dedicated mailbox, app password, plus tag **and** allowed senders,
   sender verification on.
6. Leave **Allow local network URLs** off.
7. If you do not use the Home Assistant share sheet, turn it off.
8. Keep Home Assistant and the app up to date.

## If a token leaks, or you want new ones

| Token | How to replace it | Then |
|---|---|---|
| **Device token** (generated) | Setup page, **Tokens**: open **File Transfer > Join a Network** on the X3, type its address into the box next to **New device token**, and select the button. The plugin is reinstalled with the new token right away. Without an address, install the plugin again afterwards. | On 1.6.5: enter the new token as the OPDS password on the X3. |
| **Device token** (set in the options) | Type a new value into **Device token** (`device_token`), **Save**, **Restart**. It may contain printable ASCII characters only, no spaces and none of `{`, `}`, `"` or `\`; letters and digits are easiest. (The **New device token** button is only offered for a generated token.) | Reinstall the plugin; update the OPDS password. |
| **Ingest token** | Type a new value into **Share token** (`ingest_token`), **Save**, **Restart**. Use letters, digits, `-` and `_`. | Update the token in every phone shortcut: iPhone, the Text action with the token at the top of the shortcut; Android, HTTP Shortcuts menu > **Global Variables** > `sendtox3_token`. Delete old copies of the Android import file: they contain the old token. |
| **Admin token** | Type a new value into **Admin token** (`admin_token`), **Save**, **Restart**. Use letters, digits, `-` and `_`. | All browsers signed in on the LAN are signed out. The web UI inside Home Assistant is unaffected. |

Emptying a token option does **not** create a new token: the app goes back to the
token it generated earlier. Type a new value instead, for example 32 random letters
and digits from a password manager. A value you set in the options stays in charge
from then on.

## Reporting a security problem

If you find a weakness in Send to X3 itself, please report it privately to the
maintainer of the repository you installed it from rather than in a public issue.
Problems in the X3 firmware belong to the
[CrossPoint project](https://github.com/crosspoint-reader/crosspoint-reader).
