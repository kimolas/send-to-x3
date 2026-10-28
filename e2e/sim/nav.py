"""Key sequences for the screens the e2e scenarios drive.

Every sequence is derived from the firmware's menu construction for the release (see
:mod:`e2e.sim.firmware`) and the card's contents, never from screen text. The waits
are generous upper bounds for a Mac; all network peers are on loopback.

Buttons (simulator ``HalGPIO.cpp``; firmware ``MappedInputManager.cpp``): ``DOWN``/``UP``
are the side buttons (list next/previous, reader page forward/back), ``RIGHT``/``LEFT``
the front buttons (also list next/previous via NavNext/NavPrevious), ``ENTER`` is
Confirm (acts on release), ``BACK`` is Back, ``POWER`` held > 400 ms sleeps
(``CrossPointSettings::getPowerButtonDuration``; sleep is allowed 2 s after boot,
``main.cpp:831``). The simulator's own ``SLEEP`` key is never consumed by this firmware.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path, PurePosixPath
from typing import Optional

from .firmware import NETWORK_MODES, Firmware, HomeItem
from .runner import InputScript
from .sd import browser_listing


@dataclass(frozen=True)
class Waits:
    """Milliseconds to allow after each kind of step."""

    boot: int = 2500  # first input; Home is drawn at ~0.1 s, sleep allowed from 2 s
    key: int = 450
    open_screen: int = 1200  # Home -> a list screen
    catalog_load: int = 4000  # Wi-Fi auto-join + browse request + parse
    download: int = 4000  # catalog/OPDS fetch of a small book
    opds_details: int = 2500  # 2.x publication detail page
    wifi_join: int = 4000  # File Transfer -> Join a Network -> auto-connect -> server up
    open_book: int = 5000  # reader indexing of a short book
    page_turn: int = 1500
    sleep_cycle: int = 7000  # POWER hold -> Wi-Fi join -> drain -> sleep screen

    def scaled(self, factor: float) -> "Waits":
        """Every wait multiplied by ``factor`` (>= 1 for slower hosts)."""
        if factor <= 0:
            raise ValueError("slowdown factor must be > 0")
        return replace(self, **{f.name: int(getattr(self, f.name) * factor) for f in fields(self)})

    @classmethod
    def from_env(cls) -> "Waits":
        """Defaults scaled by ``$SENDTOX3_E2E_SLOWDOWN`` (e.g. 2 on a loaded CI runner)."""
        return cls().scaled(float(os.environ.get("SENDTOX3_E2E_SLOWDOWN", "1") or 1))


class Nav:
    """Appends navigation steps to an :class:`InputScript`."""

    def __init__(self, fw: Firmware, script: InputScript, *, plugins_enabled: bool = True,
                 has_opds_servers: bool = False, recent_books: int = 0, catalog_opens: Optional[bool] = None,
                 waits: Optional[Waits] = None) -> None:
        """``recent_books``: rows Home shows for books read on this card, which come
        before its menu (at most :data:`~e2e.sim.firmware.HOME_RECENT_ROWS`).
        ``catalog_opens``: whether Confirm on a catalog row opens the book; by default
        whether the build reads ``download.open`` (every bundle sets it), False for a
        card whose device.json has none (:func:`~e2e.sim.sd.drop_download_open`)."""
        self.fw = fw
        self.s = script
        self.plugins_enabled = plugins_enabled and fw.plugins
        self.has_opds = has_opds_servers
        self.recent_books = recent_books
        self.catalog_opens = fw.opens_after_download and (catalog_opens is None or catalog_opens)
        self.w = waits or Waits.from_env()

    # -- generic --------------------------------------------------------------------------

    def select(self, index: int, *, wait_ms: Optional[int] = None, key: str = "DOWN") -> "Nav":
        """From a list whose selection is on row 0: move to ``index`` and confirm."""
        if index < 0:
            raise ValueError("row index must be >= 0")
        self.s.presses(key, index)
        self.s.press("ENTER", wait_ms=wait_ms if wait_ms is not None else self.w.open_screen)
        return self

    def back(self, *, wait_ms: Optional[int] = None) -> "Nav":
        self.s.press("BACK", wait_ms=wait_ms if wait_ms is not None else self.w.open_screen)
        return self

    # -- Home -----------------------------------------------------------------------------

    def home_index(self, item: HomeItem) -> int:
        items = self.fw.home_items(plugins_enabled=self.plugins_enabled, has_opds_servers=self.has_opds)
        if item not in items:
            raise ValueError(f"{item} is not on {self.fw.ref}'s Home menu (rows: {items})")
        # Recent books precede the menu in the selection order (HomeActivity.cpp:309-312).
        return self.recent_books + items.index(item)

    def home(self, item: HomeItem, *, wait_ms: Optional[int] = None) -> "Nav":
        """From a freshly drawn Home (selection on row 0), open ``item``."""
        return self.select(self.home_index(item), wait_ms=wait_ms)

    # -- 2.x plugins ----------------------------------------------------------------------

    def open_plugin(self, title: str, *, plugin_titles: list[str], wait_ms: Optional[int] = None) -> "Nav":
        """Home > Plugins > ``title`` (a catalog): waits for Wi-Fi and the first page."""
        self.home("plugins")
        rows = self.fw.plugin_picker_rows(has_opds_servers=self.has_opds, hub_row=False, plugin_titles=plugin_titles)
        return self.select(rows.index(title), wait_ms=wait_ms if wait_ms is not None else self.w.catalog_load)

    def catalog_fetch(self, row: int, *, wait_ms: Optional[int] = None) -> "Nav":
        """In a catalog list (selection on row 0): fetch ``row``; leaves "Download complete".

        With ``download.open`` on a build that reads it (``catalog_opens``) Confirm
        instead ends the catalog: the book downloads (or is already on the card)
        and the firmware restarts into the reader. With ``run_sim(restart="exec")`` the
        reader comes up in a relaunched process, so what follows belongs in
        ``after_restart``; otherwise it opens in the same process. Either way there is no
        "Download complete" to :meth:`dismiss`.
        """
        if wait_ms is None:
            wait_ms = self.w.download + (self.w.open_book if self.catalog_opens else 0)
        return self.select(row, wait_ms=wait_ms)

    def dismiss(self, *, wait_ms: Optional[int] = None) -> "Nav":
        """Confirm a "Download complete" (catalog: back to the list, selection kept)."""
        if self.catalog_opens:
            raise ValueError(f"{self.fw.ref} opens catalog books in the reader: there is no "
                             "\"Download complete\" screen to dismiss")
        self.s.press("ENTER", wait_ms=wait_ms if wait_ms is not None else self.w.open_screen)
        return self

    # -- OPDS -----------------------------------------------------------------------------

    def open_opds(self, *, wait_ms: Optional[int] = None) -> "Nav":
        """Open the OPDS browser on the single configured server (feed loaded after)."""
        if not self.has_opds:
            raise ValueError("no OPDS server on this card")
        load = wait_ms if wait_ms is not None else self.w.catalog_load
        if self.plugins_enabled:
            # 2.x with plugins: Home > Plugins > "OPDS Browser" (row 0), single server -> browser.
            self.home("plugins")
            return self.select(0, wait_ms=load)
        return self.home("opds", wait_ms=load)

    def opds_download(self, row: int, *, wait_ms: Optional[int] = None) -> "Nav":
        """Download the book at ``row`` of the current feed.

        Uses the front RIGHT button to move: on a paginated feed the 2.x browser
        reserves the side buttons for page links (``OpdsBookBrowserActivity.cpp:121-139``).
        2.x opens a publication page first (Confirm = Download); 1.6.5 downloads directly.
        """
        dl = wait_ms if wait_ms is not None else self.w.download
        if self.fw.version.startswith("2"):
            self.select(row, key="RIGHT", wait_ms=self.w.opds_details)
            self.s.press("ENTER", wait_ms=dl)
        else:
            self.select(row, key="RIGHT", wait_ms=dl)
        return self

    # -- File Transfer --------------------------------------------------------------------

    def file_transfer_join(self, *, wait_ms: Optional[int] = None) -> "Nav":
        """Home > File Transfer > Join a Network; the saved credential auto-joins and the
        firmware's CrossPointWebServer starts (``CrossPointWebServerActivity.cpp``)."""
        self.home("file_transfer")
        return self.select(NETWORK_MODES.index("join_network"),
                           wait_ms=wait_ms if wait_ms is not None else self.w.wifi_join)

    # -- Reading --------------------------------------------------------------------------

    def open_file(self, sd_root: Path, path: str, *, wait_ms: Optional[int] = None) -> "Nav":
        """Home > Browse Files > folders... > file, from the card's current contents."""
        parts = PurePosixPath(path).parts[1:]
        if not parts:
            raise ValueError("path must name a file")
        self.home("browse_files")
        folder = "/"
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            rows = browser_listing(sd_root, folder, self.fw)
            entry = part if last else part + "/"
            if entry not in rows:
                raise FileNotFoundError(f"{entry!r} is not listed in {folder} (rows: {rows})")
            self.select(rows.index(entry), wait_ms=(wait_ms if wait_ms is not None else self.w.open_book)
                        if last else self.w.open_screen)
            folder = folder.rstrip("/") + "/" + part
        return self

    def page_forward(self, count: int = 1, *, wait_ms: Optional[int] = None) -> "Nav":
        for _ in range(count):
            self.s.press("DOWN", wait_ms=wait_ms if wait_ms is not None else self.w.page_turn)
        return self

    # -- Power ----------------------------------------------------------------------------

    def sleep(self, *, hold_ms: int = 1500, wait_ms: Optional[int] = None) -> "Nav":
        """Hold POWER: ``enterDeepSleep`` (2.x: sleep.enter emit + Wi-Fi join + drain)."""
        self.s.hold("POWER", hold_ms, wait_ms=hold_ms + (wait_ms if wait_ms is not None else self.w.sleep_cycle))
        return self
