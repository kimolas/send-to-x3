"""What the harness needs to know about each supported CrossPoint release.

Everything here is read from the firmware source at the given tag; the file:line
references are to that tag (``$SENDTOX3_E2E_WORK/fw/<ref>/``). When adding a release,
re-check every reference before trusting the navigation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HomeItem = Literal["browse_files", "library", "plugins", "opds", "file_transfer", "settings"]


@dataclass(frozen=True)
class Firmware:
    """One firmware release as seen by the harness."""

    ref: str  # git tag in crosspoint-reader
    version: str  # what the firmware reports (User-Agent, /api/status); from platformio.ini [crosspoint]
    plugins: bool  # SD plugin system (device.json catalogs, events)
    # Reader activity that opens a .txt file. 2.x renders TXT through the EPUB engine
    # (src/activities/reader/ReaderActivity.cpp:34-39); 1.6.5 has a dedicated TxtReader.
    txt_reader_activity: str
    epub_reader_activity: str = "EpubReader"
    # Whether a sleep straight after boot can join Wi-Fi for plugin events. 2.0.0rc's
    # deliverSleepPluginEvents (src/main.cpp:405) looks up the saved credential without
    # loading wifi.json, which only WifiSelectionActivity loads (:97); so a cold-boot
    # sleep queues sleep.enter and delivers nothing until Wi-Fi is next used in a boot.
    # Fixed upstream after the tag by facaa87 "Load WiFi settings from file before sleep
    # mode" (2026-10-23).
    cold_sleep_delivery: bool = False

    def home_items(self, *, plugins_enabled: bool, has_opds_servers: bool) -> list[HomeItem]:
        """Home menu rows in order, excluding recent books.

        2.0.0rc ``HomeActivity.cpp:527-533`` and ``HomeActivity.h:57-64``: Browse Files,
        Library, [library slot], File Transfer, Settings. The library slot is "Plugins"
        when the plugin system is enabled (it then also hosts the OPDS browser row),
        otherwise "OPDS Browser" when an OPDS server is configured.
        1.6.5 ``HomeActivity.cpp:510-517``: the slot is "OPDS Browser" only.
        """
        items: list[HomeItem] = ["browse_files", "library"]
        if self.plugins and plugins_enabled:
            items.append("plugins")
        elif has_opds_servers:
            items.append("opds")
        items += ["file_transfer", "settings"]
        return items

    def plugin_picker_rows(self, *, has_opds_servers: bool, hub_row: bool, plugin_titles: list[str]) -> list[str]:
        """Rows of the Plugins picker (2.x ``PluginCatalogActivity.cpp:1600-1630``).

        "OPDS Browser" first when OPDS servers exist, then the synthetic Plugin Hub row
        (unless ``pluginHubPromptHidden`` or the hub is installed), then installed plugins
        sorted case-insensitively by title (``discoverPlugins``, ``:200-224``).
        """
        if not self.plugins:
            raise ValueError(f"{self.ref} has no plugin system")
        rows: list[str] = []
        if has_opds_servers:
            rows.append("OPDS Browser")
        if hub_row:
            rows.append("Plugin Hub")
        rows += sorted(plugin_titles, key=lambda t: (t.casefold(), t))
        return rows


FIRMWARES: dict[str, Firmware] = {
    "2.0.0rc": Firmware(ref="2.0.0rc", version="2.0.0", plugins=True, txt_reader_activity="EpubReader"),
    "1.6.5": Firmware(ref="1.6.5", version="1.6.5", plugins=False, txt_reader_activity="TxtReader"),
    # Optional: a develop snapshot (2026-10-25) that contains facaa87, to show the
    # cold-boot zero-tap delivery the next release will have. Not a release.
    "3f387a9": Firmware(ref="3f387a9", version="2.0.0", plugins=True, txt_reader_activity="EpubReader",
                        cold_sleep_delivery=True),
}

#: Firmware refs the suite runs by default (releases users actually have).
RELEASES = ("2.0.0rc", "1.6.5")

#: Network-mode menu (``NetworkModeSelectionActivity.cpp:13-20``, same in both releases).
NETWORK_MODES = ["join_network", "calibre_wireless", "create_hotspot"]

#: The SSID the simulator's fake Wi-Fi reports by default (crosspoint-simulator src/WiFi.h).
SIM_SSID = "Simulator WiFi (fake)"


def firmware(ref: str) -> Firmware:
    """The profile for ``ref`` (``KeyError`` with the supported list otherwise)."""
    try:
        return FIRMWARES[ref]
    except KeyError:
        raise KeyError(f"unsupported firmware ref {ref!r}; known: {', '.join(FIRMWARES)}") from None
