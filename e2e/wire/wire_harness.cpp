// Wire harness: the CrossPoint 2.x plugin HTTP path, compiled for the host, talking to
// a real server over real TCP sockets (no curl shim, unlike the simulator).
//
// Compiled firmware units (unmodified, from the checkout given to build_wire.sh):
//   src/util/PluginHttp.cpp, src/util/PluginEvents.cpp, src/util/PluginLocations.cpp,
//   src/util/StringUtils.cpp, lib/Utf8/Utf8.cpp, src/network/HttpDownloader.cpp,
//   freeink-sdk SecureNet (SecureHttpClient.h, ResumableFetch.h, SecureClient.cpp; plain
//   http only, no wolfSSL), CatalogList/JsonListParser.cpp, JsonSax/StreamingJsonParser.cpp.
// Host stand-ins: crosspoint-simulator HalStorage.cpp (a folder as the SD card) and
// Arduino/WString shims, plus e2e/wire/stubs (POSIX-socket WiFiClient, Logging, toast-only
// UITheme, LibraryBuilder, CrossPointSettings, esp_wifi, esp_random).
//
// The `catalog` and `download` commands mirror PluginCatalogActivity at 2.0.0rc
// (loadManifest :226-328, substituted :396-418, downloadDir :420-431,
// fetchBrowseResponse :637-671, parseBrowseResponse :752-837, downloadBook :1041-1138),
// without the UI, compatibility filtering, lists, search, auth flows or bundles.
// `sleep` and `drain` run the real pluginevents::emit/drain.
//
// Usage (SD card root in $CROSSPOINT_SIM_SD):
//   wire_harness catalog <plugin dir on SD> [page]
//   wire_harness download <plugin dir on SD> <item id> [page]
//   wire_harness sleep [book-path percent]
//   wire_harness drain
// Output: one JSON object per line prefixed with '@' (machine readable); firmware log
// lines go to stderr.
#include <ArduinoJson.h>
#include <HalStorage.h>
#include <HardwareSerial.h>
#include <JsonListParser.h>
#include <Logging.h>
#include <SecureHttpClient.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "network/HttpDownloader.h"
#include "network/ProtectedPaths.h"
#include "util/FirmwareIdentity.h"
#include "util/PluginEvents.h"
#include "util/PluginHttp.h"
#include "util/StringUtils.h"

ESPMock ESP;
HWCDC Serial;

namespace {
constexpr char BROWSE_TMP_PATH[] = "/.pcat_tmp.json";  // PluginCatalogActivity.cpp:63
constexpr size_t MAX_MANIFEST_SIZE = 8 * 1024;
constexpr size_t MAX_BROWSE_RESPONSE = 1024 * 1024;
constexpr int MAX_PAGE_SIZE = 16;

std::string jsonStr(const std::string& s) {
  JsonDocument d;
  d.set(s.c_str());
  std::string out;
  serializeJson(d, out);
  return out;
}

void emitWire() {
  // Binary-safe: base64 of every byte the device wrote, plus a printable copy.
  const std::string& sent = WireTrace::sent();
  static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string b64;
  for (size_t i = 0; i < sent.size(); i += 3) {
    const uint32_t n = (static_cast<uint8_t>(sent[i]) << 16) |
                       (i + 1 < sent.size() ? static_cast<uint8_t>(sent[i + 1]) << 8 : 0) |
                       (i + 2 < sent.size() ? static_cast<uint8_t>(sent[i + 2]) : 0);
    b64 += B64[(n >> 18) & 63];
    b64 += B64[(n >> 12) & 63];
    b64 += i + 1 < sent.size() ? B64[(n >> 6) & 63] : '=';
    b64 += i + 2 < sent.size() ? B64[n & 63] : '=';
  }
  printf("@{\"type\":\"wire\",\"connects\":%d,\"sent_b64\":\"%s\"}\n", WireTrace::connects(), b64.c_str());
}

struct Manifest {
  std::string tokenFile, tokenPath, configFile, browseFormat;
  pluginhttp::RequestSpec browseReq, downloadReq;
  std::string itemsPath, titlePath, authorPath, idPath, urlPath, versionPath, bundleBasePath, bundleFilesPath;
  std::string destDir, filenameTpl, sidecarPath, sidecarBody, dlUrlPath, dlUser, dlPass;
  int pageSize = 8;
};

bool loadManifest(const std::string& manifestPath, Manifest& m, std::string& why) {
  std::string raw;
  if (!Storage.readFileToString("PCAT", manifestPath, MAX_MANIFEST_SIZE, raw)) {
    why = "unreadable or over 8 KiB";
    return false;
  }
  JsonDocument doc;
  if (deserializeJson(doc, raw) != DeserializationError::Ok) {
    why = "not strict JSON";
    return false;
  }
  const std::string pluginDir = manifestPath.substr(0, manifestPath.rfind('/'));
  m.tokenFile = pluginhttp::inPluginDir(pluginDir, doc["token"]["file"] | "");
  m.tokenPath = doc["token"]["path"] | "token";
  m.configFile = pluginhttp::inPluginDir(pluginDir, doc["config"]["file"] | "");
  JsonVariantConst browse = doc["browse"];
  m.browseFormat = browse["format"] | "json";
  if (m.browseFormat != "json") {
    why = "only JSON catalogs are mirrored by the harness";
    return false;
  }
  pluginhttp::readRequest(browse, "GET", m.browseReq);
  m.itemsPath = browse["items"] | "";
  m.titlePath = browse["fields"]["title"] | "title";
  m.authorPath = browse["fields"]["author"] | "";
  m.idPath = browse["fields"]["id"] | "";
  m.urlPath = browse["fields"]["url"] | "";
  m.versionPath = browse["fields"]["version"] | "";
  m.pageSize = std::clamp(static_cast<int>(browse["page_size"] | 8), 1, MAX_PAGE_SIZE);
  JsonVariantConst dl = doc["download"];
  pluginhttp::readRequest(dl, "GET", m.downloadReq);
  m.dlUrlPath = dl["url_path"] | "";
  m.dlUser = dl["username"] | "";
  m.dlPass = dl["password"] | "";
  m.destDir = pluginhttp::inPluginDir(pluginDir, dl["dest_dir"] | "");
  if (!m.destDir.empty() && !protectedpaths::isPluginPath(m.destDir)) {
    why = "dest_dir outside plugin space";
    return false;
  }
  m.filenameTpl = dl["filename"] | "{title}.epub";
  m.bundleBasePath = dl["bundle"]["base"] | "";
  m.bundleFilesPath = dl["bundle"]["files"] | "";
  m.sidecarPath = dl["sidecar"]["path"] | "";
  m.sidecarBody = dl["sidecar"]["body"] | "";
  if (m.browseReq.url.empty()) {
    why = "browse.url missing";
    return false;
  }
  return true;
}

struct Item {
  std::string title, author, id, url;
};

struct Catalog {
  Manifest m;
  std::string token;
  pluginhttp::Headers config;
  int page = 1;

  std::string substituted(std::string tpl, const Item* item) const {
    pluginhttp::substituteAll(tpl, "{token}", token);
    for (const auto& kv : config) pluginhttp::substituteAll(tpl, ("{cfg." + kv.first + "}").c_str(), kv.second);
    pluginhttp::substituteAll(tpl, "{firmware_family}", firmwareidentity::FAMILY);
    pluginhttp::substituteAll(tpl, "{firmware_version}", CROSSPOINT_VERSION);
    pluginhttp::substituteAll(tpl, "{plugin_capabilities}", firmwareidentity::PLUGIN_CAPABILITIES);
    char num[16];
    snprintf(num, sizeof(num), "%d", firmwareidentity::PLUGIN_API_VERSION);
    pluginhttp::substituteAll(tpl, "{plugin_api}", num);
    snprintf(num, sizeof(num), "%d", page);
    pluginhttp::substituteAll(tpl, "{page}", num);
    snprintf(num, sizeof(num), "%d", m.pageSize + 1);
    pluginhttp::substituteAll(tpl, "{limit}", num);
    pluginhttp::substituteAll(tpl, "{query}", "");
    pluginhttp::substituteAll(tpl, "{query_raw}", "");
    if (item) {
      pluginhttp::substituteAll(tpl, "{id}", item->id);
      pluginhttp::substituteAll(tpl, "{title}", item->title);
      pluginhttp::substituteAll(tpl, "{author}", item->author);
      pluginhttp::substituteAll(tpl, "{url}", item->url);
    }
    return tpl;
  }

  pluginhttp::Headers substitutedHeaders(const pluginhttp::Headers& headers, const Item* item) const {
    pluginhttp::Headers out;
    for (const auto& h : headers) out.emplace_back(h.first, substituted(h.second, item));
    return out;
  }

  std::string downloadDir() const {
    for (const auto& kv : config) {
      if (kv.first != "dest_dir" || kv.second.empty()) continue;
      std::string dir = kv.second.front() == '/' ? kv.second : "/" + kv.second;
      while (dir.size() > 1 && dir.back() == '/') dir.pop_back();
      if (dir == "/") return "";
      if (protectedpaths::isPluginPath(dir)) return dir;
    }
    return m.destDir;
  }
};

// Browse one page and parse it; returns the process exit code (0 = ok).
int browse(Catalog& c, freeink::SecureHttpClient& session, std::vector<Item>& items, bool& hasMore) {
  pluginhttp::loadConfigFile(c.m.configFile, c.config);
  if (!pluginhttp::loadTokenFromFile(c.m.tokenFile, c.m.tokenPath, c.token)) {
    printf("@{\"type\":\"browse\",\"state\":\"NO_TOKEN\"}\n");
    return 3;
  }
  const std::string url = c.substituted(c.m.browseReq.url, nullptr);
  const int status = pluginhttp::requestToFile(&session, url, c.m.browseReq.method,
                                               c.substituted(c.m.browseReq.body, nullptr),
                                               c.substitutedHeaders(c.m.browseReq.headers, nullptr), BROWSE_TMP_PATH,
                                               MAX_BROWSE_RESPONSE);
  if (status < 200 || status >= 300) {
    Storage.remove(BROWSE_TMP_PATH);
    const char* state = (status == 401 || status == 403) ? "NO_TOKEN" : "FETCH_FEED_FAILED";
    printf("@{\"type\":\"browse\",\"status\":%d,\"url\":%s,\"state\":\"%s\"}\n", status, jsonStr(url).c_str(), state);
    return 4;
  }
  items.clear();
  auto append = [&](JsonListParser::Row& row) {
    if (row.field[JsonListParser::F_TITLE].empty() || static_cast<int>(items.size()) > c.m.pageSize) return;
    items.push_back({row.field[JsonListParser::F_TITLE], row.field[JsonListParser::F_AUTHOR],
                     row.field[JsonListParser::F_ID], row.field[JsonListParser::F_URL]});
  };
  const std::string* const fields[] = {&c.m.titlePath, &c.m.authorPath,  &c.m.idPath,
                                       &c.m.urlPath,   &c.m.versionPath, &c.m.bundleBasePath};
  auto parser = std::make_unique<JsonListParser>(
      c.m.itemsPath, fields, c.m.bundleFilesPath,
      [](void* ctx, JsonListParser::Row& row) { (*static_cast<decltype(append)*>(ctx))(row); }, &append);
  HalFile f;
  bool ok = Storage.openFileForRead("PCAT", BROWSE_TMP_PATH, f) &&
            parser->parse([](void* fp, char* buf, size_t len) { return static_cast<HalFile*>(fp)->read(buf, len); },
                          &f);
  f.close();
  if (!ok) {
    printf("@{\"type\":\"browse\",\"status\":%d,\"url\":%s,\"state\":\"PARSE_FEED_FAILED\"}\n", status,
           jsonStr(url).c_str());
    return 5;
  }
  hasMore = static_cast<int>(items.size()) > c.m.pageSize;
  if (hasMore) items.resize(c.m.pageSize);
  printf("@{\"type\":\"browse\",\"status\":%d,\"url\":%s,\"rows\":%zu,\"has_more\":%s,\"state\":\"BROWSING\"}\n",
         status, jsonStr(url).c_str(), items.size(), hasMore ? "true" : "false");
  for (size_t i = 0; i < items.size(); i++) {
    printf("@{\"type\":\"row\",\"index\":%zu,\"id\":%s,\"title\":%s,\"author\":%s,\"url\":%s}\n", i,
           jsonStr(items[i].id).c_str(), jsonStr(items[i].title).c_str(), jsonStr(items[i].author).c_str(),
           jsonStr(items[i].url).c_str());
  }
  return 0;
}

int download(Catalog& c, const Item& item) {
  std::string fileUrl;
  if (!c.m.dlUrlPath.empty()) {
    printf("@{\"type\":\"download\",\"result\":\"UNSUPPORTED\",\"why\":\"url_path hop not mirrored\"}\n");
    return 6;
  }
  fileUrl = c.substituted(c.m.downloadReq.url, &item);
  const auto report = [&](const char* result, const std::string& dest, int http) {
    printf("@{\"type\":\"download\",\"id\":%s,\"url\":%s,\"dest\":%s,\"result\":\"%s\",\"http\":%d}\n",
           jsonStr(item.id).c_str(), jsonStr(fileUrl).c_str(), jsonStr(dest).c_str(), result, http);
  };
  if (fileUrl.empty()) {
    report("FILE_ERROR", "", 0);
    return 7;
  }
  const std::string folderDir = c.downloadDir();
  bool haveFolder = !folderDir.empty();
  if (haveFolder && !Storage.exists(folderDir.c_str()) && !Storage.mkdir(folderDir.c_str())) haveFolder = false;
  const std::string filename =
      StringUtils::sanitizeFilenamePreservingExtension(c.substituted(c.m.filenameTpl, &item));
  if (filename.empty() || filename.find("..") != std::string::npos || filename.find('/') != std::string::npos) {
    report("FILE_ERROR(unsafe filename)", filename, 0);
    return 7;
  }
  std::string dest = (haveFolder ? folderDir : std::string()) + "/" + filename;
  if (!protectedpaths::isPluginPath(dest)) {
    report("FILE_ERROR(unsafe dest)", dest, 0);
    return 7;
  }
  const auto result = HttpDownloader::downloadToFile(fileUrl, dest, nullptr, nullptr, c.substituted(c.m.dlUser, &item),
                                                     c.substituted(c.m.dlPass, &item),
                                                     c.substitutedHeaders(c.m.downloadReq.headers, &item));
  static const char* NAMES[] = {"OK", "HTTP_ERROR", "FILE_ERROR", "ABORTED", "UNAUTHORIZED"};
  report(NAMES[static_cast<int>(result)], dest, HttpDownloader::lastStatus());
  return result == HttpDownloader::OK ? 0 : 8;
}

int cmdCatalog(const std::string& pluginDir, int page, const char* wantId) {
  Catalog c;
  c.page = page;
  std::string why;
  if (!loadManifest(pluginDir + "/device.json", c.m, why)) {
    printf("@{\"type\":\"manifest\",\"ok\":false,\"why\":%s}\n", jsonStr(why).c_str());
    return 2;
  }
  freeink::SecureHttpClient session;  // one reused session per catalog (PluginCatalogActivity.cpp:499-501)
  session.setReuse(true);
  std::vector<Item> items;
  bool hasMore = false;
  if (const int rc = browse(c, session, items, hasMore)) return rc;
  if (!wantId) return 0;
  for (const auto& item : items) {
    if (item.id == wantId) return download(c, item);
  }
  printf("@{\"type\":\"download\",\"id\":%s,\"result\":\"NOT_LISTED\"}\n", jsonStr(wantId).c_str());
  return 9;
}
}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: wire_harness catalog <plugin-dir> [page] | download <plugin-dir> <id> [page] | "
                    "sleep [book percent] | drain\n");
    return 64;
  }
  Storage.begin();
  const std::string cmd = argv[1];
  int rc = 0;
  if (cmd == "catalog" && argc >= 3) {
    rc = cmdCatalog(argv[2], argc >= 4 ? std::max(1, atoi(argv[3])) : 1, nullptr);
  } else if (cmd == "download" && argc >= 4) {
    rc = cmdCatalog(argv[2], argc >= 5 ? std::max(1, atoi(argv[4])) : 1, argv[3]);
  } else if (cmd == "sleep" || cmd == "drain") {
    pluginevents::refreshSubscriptions();
    if (cmd == "sleep") {
      // main.cpp:376-421 with Wi-Fi up: emit sleep.enter (with the reader vars when given), then drain.
      pluginevents::Var vars[2];
      size_t n = 0;
      if (argc >= 4) {
        vars[n++] = {"book", argv[2]};
        vars[n++] = {"percent", argv[3]};
      }
      pluginevents::emit(pluginevents::Event::SleepEnter, vars, n);
      printf("@{\"type\":\"emit\",\"event\":\"sleep.enter\",\"wants_connect\":%s}\n",
             pluginevents::wantsConnectAny() ? "true" : "false");
    }
    pluginevents::drain(nullptr);
    printf("@{\"type\":\"drain\",\"done\":true}\n");
  } else {
    fprintf(stderr, "unknown command or missing arguments: %s\n", cmd.c_str());
    return 64;
  }
  emitWire();
  return rc;
}
