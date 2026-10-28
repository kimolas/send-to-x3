# Using Send to X3

Daily use, once [setup](SETUP.md) is done. Example addresses: the Pi is
`192.168.1.50`, the X3 is `192.168.1.60`.

## The daily routine

| Your setup | You do | It arrives |
|---|---|---|
| CrossPoint 2.x (the default) | Share from the phone, then on the X3 open **Home > Plugins > Send to X3** and press **Confirm** on the first row, **Get 3 new** | Right away: one EPUB with everything new (one new item keeps its own title) |
| The [Send to X3 firmware](FIRMWARE.md) | The same; the button says **Open** | Right away, and the book opens in the reader |
| CrossPoint 2.x, one item | Share, then **Home > Plugins > Send to X3** and select the item below the first row | Right away, one file for that item (opened in the reader on the Send to X3 firmware) |
| CrossPoint 2.x, with the optional delivery at sleep | Share, then put the X3 to sleep at home (on 2.0.0rc: open the plugin first) | At the next sleep: one EPUB with everything new |
| CrossPoint 1.6.5, OPDS | Share, then **Home > OPDS Browser** and select the first entry, **Get 3 new**, or a single item | Right away |
| Any firmware, LAN push | Share, then on the X3 open **File Transfer > Join a Network** for half a minute | All new items, one file each |

## What you can send, and what arrives

| You share | What you get on the X3 |
|---|---|
| **A link** to an article | The article's text with its title, author, site and date. No images, ads, menus, comments or links. |
| **A link with a short comment** of up to 30 words (for example "read this before Friday") | The article, with your comment at the top, under the title: `Note: Comment: read this before Friday`. Share-sheet filler ("Check out this article", "Shared via ...") and the page's own title are not kept as a comment. Sharing the same link again with a new comment gives a new item. |
| **Several links at once** (2 to 10, with at most 30 other words) | One item per link (your comment, if any, on each). More links, or more text, become one note. |
| **A web page with text selected** (Safari's Share button) | Just the selected text, with the page as its source. |
| **Selected or typed text** without a link | A note. Its title is the first line or sentence. |
| **A PDF** (email attachment, upload in the web UI, or a link to a PDF) | The PDF's text, reflowed into paragraphs and headings. See [PDFs](pdf.md). |
| **An email** (forwarded newsletter, or your own text) | The text of the mail, with the subject as its title. A mail that contains 1 to 10 links and at most 30 words of your own becomes the article(s), with your words kept as a comment. Each PDF, TXT or HTML attachment becomes its own item (up to 50 items per mail). |
| **An EPUB file** (upload in the web UI) | The EPUB as it is. |
| **The same link again within 24 hours** | Nothing new: the reply says "Already in the queue: <title>". |
| **A page that cannot be read** (paywall, login, bot check, a page that needs JavaScript) | Still an item: the title, the link and a short explanation, plus whatever text was available. It shows as **failed** in the web UI. |
| **Only an image** | Nothing; there is no text to send. |

The phone shows the result: "Sent to X3: <title>" when it worked, "Saved, but could not
extract the text: ..." when the page could not be read. See the phone guides for
[iPhone](phone-ios.md) and [Android](phone-android.md).

**Slow shares.** Most shares take a few seconds. Large pages and PDFs can take longer
on a Raspberry Pi. After 25 seconds the phone gets an answer saying the share was
received and is "still processing"; the item appears in the queue (and on the X3)
shortly afterwards. A share gets at most 2.5 minutes (11 minutes with **OCR scanned
PDFs** on); after that it is kept as a **failed** item that says why (for example
"Converting this PDF took longer than 140 seconds, so it was stopped." or "Timed out
after 150 s while fetching the page"). If the app restarts or the Pi loses
power while a share is still being processed, the app picks it up again after the
restart (a share that stops the app three times is kept as a failed item instead).

**When the app cannot save.** If the Pi's disk is full or the database fails, the
phone gets "Not saved: the server could not store it (...). Try again later." and the
Queue page shows a warning. Nothing was saved; free some space and share again.

## How it looks on the X3

Every item is an EPUB by default. Each one starts with the title, the author in
italics, the site and date in italics, any notes (such as your comment, or "Marked
subscriber-only by the publisher: this may be only the free preview."), and a line;
then the text, and at the end
`Source: <site>`. Long addresses are never printed in the text (they would overflow
the lines): a link longer than 30 characters shows as `[example.com]`. Very long
articles are split into several chapters. The Library shows the item's title and
author (or site).

Inside the book the title is the article's real headline, with a trailing site name
removed ("Story | The Guardian" becomes "Story"). The file name, the catalog, OPDS and
the web UI use a version cleaned so the X3 accepts it as a file name: for example
`...` becomes `…`, `/` or `:` become dashes, `?` is dropped, and it is cut to 90
bytes. Emoji are removed everywhere because the X3's fonts cannot show them. A title
that is already taken gets ` (2)`, ` (3)` and so on; titles of items you fetched from
the catalog or OPDS, and the names LAN push saved, stay taken for good, even after the
item is deleted, so a new item never replaces (or, on the Send to X3 firmware, opens)
a file already on the X3.

With **Download format** (`delivery_format`) set to `txt`, catalog downloads and LAN
push give plain `.txt` files instead. They show the title as the first line, then
`<byline> · <site> · <date>`, a line `———`, one paragraph per line with a blank line
between, and `Source: <full address>` at the end. The X3 cannot justify `.txt` text
and its Library uses the file name as the title, which is why EPUB is the default.
Delivery at sleep and OPDS always use EPUB.

## The four ways onto the X3

| Way | Firmware | What happens | File on the SD card |
|---|---|---|---|
| **Get 3 new** | 2.x plugin, OPDS | The first row of **Home > Plugins > Send to X3** (and the first OPDS entry when something is new) downloads everything new as one EPUB. | `/Inbox/Get 3 new · Oct 26 14.05.epub` (OPDS: `Get 3 new · Oct 26 14.05.epub` in the OPDS download folder) |
| **Catalog** | 2.x plugin | Below the first row, **Home > Plugins > Send to X3** lists everything in the queue, newest first, 8 rows per page with **Next Page**. You select an item to fetch it. | `/Inbox/<title>.epub` |
| **OPDS** | 1.6.5 and 2.x | **OPDS Browser** lists everything, newest first, 25 per page. You select an item to download it. | `<site> - <title>.epub` in the OPDS download folder |
| **Delivery at sleep** (optional, off by default) | 2.x plugin | When the X3 goes to sleep on your home Wi-Fi, it fetches everything new as one EPUB. | `/Inbox/<numbers>.epub` |
| **LAN push** | 1.6.5 and 2.x | While **File Transfer** is open, the app copies every new item onto the X3. | `/Inbox/<title>.epub`, or `<title> (2).epub` if the name is taken |

How they work together:

- **"New" means "not yet on the X3 by any way".** **Get 3 new**, delivery at sleep and
  LAN push only send new items. An item you already fetched from the catalog or OPDS
  is not in the next **Get 3 new**, and is not sent again at sleep or by push.
- **The catalog and OPDS always list everything** you shared (within the retention
  period), so you can fetch an item again at any time. In the catalog the line under
  each title starts with `New` for items that never reached the X3, and with
  `Read on X3 <date>` for items that were delivered on that date (by any way), followed
  by the word count and the site.
- **Fetching the same item again** from the catalog or OPDS replaces the file and
  starts it from the beginning (the X3 forgets the reading position of a replaced
  file). On the Send to X3 firmware, **Open** on an item whose own row saved it
  before (the same file name in `/Inbox`) opens that copy where you left off instead,
  without downloading it again.
- The web UI's **On the X3** column shows how each item arrived: `fetch` (**Get 3
  new**), `catalog`, `auto` (delivery at sleep), `opds` or `push`, with the time.
  **waiting** means not yet.

### Get 3 new in detail

- The row's title is the number of new items and the time of the newest share, for
  example **Get 3 new · Oct 26 14.05**. It is also the file name. A name is never used
  twice: if two fetches would get the same one (several uploaded EPUBs shared in the
  same minute, which always go one per book), the later one ends in ` (2)`, ` (3)`, so
  a later fetch never replaces an earlier book. The line below the title lists the new
  titles, newest first.
- With the EPUB format (the default), several items arrive as one book titled
  `Inbox: <n> items (<date> <time>)`, one chapter per item, oldest first; jump between
  them with **Select Chapter** in the reader. One new item arrives as itself, with its
  own title in the Library. With **Download format** `txt` you get one `.txt` file with
  the items one after the other, and the Library shows its file name.
- One fetch holds up to about 900 KB of books; anything beyond is in the next **Get N
  new**. Uploaded EPUBs always come one per fetch. A single item larger than 900 KB
  still comes whole.
- **The X3 confirms what it saved.** Items count as delivered only once the X3 reports
  the saved file. It does so the next time it joins Wi-Fi: after you go back to Home
  (the X3 restarts) and open **Send to X3** again, before the list loads. Going back
  only to the plugin list and opening it again keeps Wi-Fi on, so nothing is reported
  yet. (That is why **Home > Plugins** says "Receives: downloads" under the plugin.)
  The Send to X3 firmware reports right after the download, before it opens the book.
- Until the report arrives the web UI shows **sent ..., not confirmed yet**, and the
  first row reads **Nothing new right now** with "Your last fetch (...) is confirmed the
  next time the X3 joins Wi-Fi", so pressing Confirm out of habit never replaces the
  book you are reading. Should no report come within two hours, the same items are
  offered again under a new name (a second copy, never a replacement).
- If a download is cancelled (**Back**) or the Wi-Fi drops before the X3 has the whole
  book, the same items are offered again at once, with ` (2)` added to the name: a name
  that was ever sent is never offered again, so no press can replace a book on the X3.
- With nothing new the row reads **Nothing new right now**. Selecting it shows
  **Download failed** and changes nothing (on the Send to X3 firmware too, where the
  button says **Open**).
- If the app cannot prepare the download (for example the Home Assistant disk is full)
  the first row reads **New reading could not be prepared**; the items below it still
  download one by one, and the app's log says what went wrong.
- Something you share while the list is open simply waits for the next **Get N new**.
  Pressing an old **Get N new** row can show **Download failed** when the list was
  loaded again elsewhere in the meantime (the OPDS feed, or another page 1) with
  different new items; go back and open **Send to X3** again.

### Reading right from the plugin (Send to X3 firmware)

On the [Send to X3 firmware](FIRMWARE.md) the button on every row of **Home > Plugins >
Send to X3** says **Open** instead of **Fetch**:

- **Open** on **Get 3 new** (or on a single item): **Downloading...**, then a short
  **Loading** message, and the X3 restarts straight into the book. The X3 reports the
  file to the app before it restarts, so the web UI shows `fetch` (or `catalog`) at
  once and the next **Get N new** never repeats it.
- **Open** on an item whose own row saved it before (same file name in `/Inbox`) opens
  that copy where you left off; nothing is downloaded. A **Get N new** row always has a
  new name, so it always downloads.
- To go back to a **Get N new** book, open it from Home (recent books) or the
  **Library**, where it keeps your page. **Open** on one of its items' own rows looks
  for a file named after the item, so it downloads that item as a separate file,
  which starts at page 1.
- A cancelled or failed download opens nothing; the items are offered again as usual.
- On official CrossPoint the same plugin works as described above: the button says
  **Fetch** and the book waits in the Library.
- Only **Open** opens books: deliveries at sleep and by LAN push wait in the Library
  as usual.

Note: this firmware was tried in the X3 simulator, not yet on a real X3; see
[The Send to X3 firmware](FIRMWARE.md).

### Delivery at sleep in detail

Off by default; [Setup, Part 5a step 7](SETUP.md#step-7-optional-automatic-delivery-at-sleep)
turns it on.


- One sleep, one EPUB. One new item: the EPUB is titled like the item. Several: the
  EPUB is titled `Inbox: <n> items (<date> <time>)` and each item is a chapter, so
  you can jump between them with **Select Chapter** in the reader.
- One delivery holds up to about 900 KB; anything beyond waits for the next sleep.
- A single item larger than the X3 accepts at sleep (about 1 MB) arrives as a short
  note, "Too large to deliver: <title>", telling you to open it from the catalog. The
  item itself still counts as new: the catalog shows it as `New`, LAN push still sends
  it, and later sleeps skip it.
- You see a short **Loading...** popup, then **Send to X3: new reading delivered**,
  then **Going to sleep**. With nothing new, the X3 just goes to sleep.
- It needs a battery of 20 % or more and the saved home Wi-Fi password.
- Away from home the X3 still tries at every sleep, which makes it slower to fall
  asleep (up to about 10 seconds; longer if it is on another Wi-Fi that it knows).
  Items wait in the queue until you are back.
- **On 2.0.0rc** a plain wake, read, sleep does not deliver; see the caveat in
  [Setup, Part 5a step 7](SETUP.md#step-7-optional-automatic-delivery-at-sleep).

The web UI's **Channels** box lists **Recent automatic deliveries** with the number of
items in each.

## PDFs

PDFs become reflowable text: columns are read in order, running headers and page
numbers are removed, footnotes are collected at the end, and images are left out.
Scanned PDFs without a text layer arrive as a short note ("Scanned PDF: no text
layer") unless you turn on **OCR scanned PDFs** (`pdf_ocr`). OCR is slow on a Pi (about
10 to 20 seconds per page, at most 20 pages per PDF) and makes some mistakes. The
[PDF guide](pdf.md) has all the details and limits.

## The web UI

Open it from the Home Assistant sidebar (**Send to X3**) or with **Open Web UI** on the
app's page. From another computer on your network you can also open
`http://192.168.1.50:8765/admin?token=<admin token>` once; your browser then stays
signed in for 30 days (**Sign out** at the top ends it).

**Queue page:**

- **Send something:** paste text or a link (optional **Title**) and select **Send to
  X3**, or choose a file under **Or upload a file** (PDF, TXT, HTML or EPUB, up to 30
  MB) and select **Upload**.
- **Channels:** the state of each way in and out: **X3 plugin (CrossPoint 2.x)**,
  **OPDS feed (CrossPoint 1.6.5+)**, **LAN push to the X3**, **Email (IMAP)** and
  **Home Assistant share sheet**. Each shows **ok**, **waiting**, **starting**,
  **warning**, **error** or **disabled**, with a short explanation.
- **Queue:** every item, newest first, 50 per page. Under each title: the kind (`url`,
  `text`, `email`, `pdf`), where it came from and the word count. **Status** is
  **ready** or **failed** (with the reason).

**Item page** (select a title): the details, the files (**EPUB** and **TXT** downloads
with their sizes), a **Preview** of the text, and the buttons **Deliver again** and
**Delete**.

## Sending something again, deleting

- **Requeue** (in the Queue table, for items that already reached the X3) or **Deliver
  again** (on the item page) forgets that the item was delivered. It counts as new
  again: the next **Get 3 new** (under a new name, so the copy already on the X3 stays),
  delivery at sleep or LAN push sends it once more, and the catalog shows `New`.
- **Delete** removes the item from Home Assistant (after a confirmation). Copies already
  on the X3 stay there, and so does its name (see [How it looks on the
  X3](#how-it-looks-on-the-x3)).
- **Deleting files on the X3:** open **File Transfer > Join a Network** on the X3, open
  the address it shows in a browser, go to **File Manager**, tick the files and use the
  delete button.

## How long items are kept

Items older than **Keep items for (days)** (`retention_days`, default 30) are deleted
from Home Assistant once a day. Files already on the X3 are never touched. Items and
generated tokens live in the app's data folder and are part of your Home Assistant
backups. While Home Assistant makes a backup, the app is stopped for a moment; a phone
share sent then fails and has to be sent again (mail simply waits).

## Sending from away

- **Email** works from anywhere (see [Setup, Part 6](SETUP.md#part-6-email-optional-works-from-anywhere)).
  The iPhone shortcut in `auto` mode switches to email by itself when you are not on
  your home Wi-Fi; the Android shortcut offers an email when it cannot reach the app.
- **The Home Assistant Companion app** share works wherever the Companion app reaches
  your Home Assistant.
- The share address `http://192.168.1.50:8765` only works at home. Do not forward that
  port on your router; see [Security](SECURITY.md).
