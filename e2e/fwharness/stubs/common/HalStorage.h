#pragma once
// Host (POSIX) stand-in for lib/hal/HalStorage.h, covering the subset used by Epub.cpp,
// BookMetadataCache.cpp, CssParser.cpp, ZipFile.cpp, Txt.cpp and the parsers.
// Firmware paths are absolute ("/.crosspoint/..."); they are mapped under $CP_SDROOT.
#include <Arduino.h>
#include <Print.h>
#include <WString.h>
#include <sys/stat.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>

class HalFile : public Print {
 public:
  HalFile() = default;
  ~HalFile() override { close(); }
  HalFile(const HalFile&) = delete;
  HalFile& operator=(const HalFile&) = delete;
  HalFile(HalFile&& o) noexcept : f_(o.f_) { o.f_ = nullptr; }
  HalFile& operator=(HalFile&& o) noexcept {
    if (this != &o) {
      close();
      f_ = o.f_;
      o.f_ = nullptr;
    }
    return *this;
  }
  bool openPath(const char* p, const char* mode) {
    close();
    f_ = std::fopen(p, mode);
    return f_ != nullptr;
  }
  void flush() {
    if (f_) std::fflush(f_);
  }
  size_t size() const {
    if (!f_) return 0;
    long o = std::ftell(f_);
    std::fseek(f_, 0, SEEK_END);
    long e = std::ftell(f_);
    std::fseek(f_, o, SEEK_SET);
    return e > 0 ? (size_t)e : 0;
  }
  size_t fileSize() const { return size(); }
  bool seek(size_t pos) { return f_ && std::fseek(f_, (long)pos, SEEK_SET) == 0; }
  bool seekSet(size_t pos) { return seek(pos); }
  bool seekCur(int64_t off) { return f_ && std::fseek(f_, (long)off, SEEK_CUR) == 0; }
  int available() const { return f_ ? (int)(size() - position()) : 0; }
  size_t position() const { return f_ ? (size_t)std::ftell(f_) : 0; }
  int read(void* buf, size_t n) { return f_ ? (int)std::fread(buf, 1, n, f_) : -1; }
  int read() {
    unsigned char c;
    return read(&c, 1) == 1 ? c : -1;
  }
  size_t write(const uint8_t* buf, size_t n) override { return f_ ? std::fwrite(buf, 1, n, f_) : 0; }
  size_t write(const void* buf, size_t n) { return write(static_cast<const uint8_t*>(buf), n); }
  size_t write(uint8_t b) override { return write(&b, 1); }
  bool close() {
    if (!f_) return false;
    bool ok = std::fclose(f_) == 0;
    f_ = nullptr;
    return ok;
  }
  bool isOpen() const { return f_ != nullptr; }
  operator bool() const { return isOpen(); }

 private:
  std::FILE* f_ = nullptr;
};

class HalStorage {
 public:
  static HalStorage& getInstance() {
    static HalStorage s;
    return s;
  }
  static std::string map(const std::string& p) {
    const char* root = std::getenv("CP_SDROOT");
    std::string r = root ? root : "./sdroot";
    if (!p.empty() && p[0] == '/') return r + p;
    return p;  // relative paths are used verbatim
  }
  bool openFileForRead(const char*, const std::string& p, HalFile& f) { return f.openPath(map(p).c_str(), "rb"); }
  bool openFileForRead(const char* m, const char* p, HalFile& f) { return openFileForRead(m, std::string(p), f); }
  bool openFileForWrite(const char*, const std::string& p, HalFile& f) { return f.openPath(map(p).c_str(), "wb"); }
  bool openFileForWrite(const char* m, const char* p, HalFile& f) { return openFileForWrite(m, std::string(p), f); }
  bool exists(const char* p) {
    struct stat st;
    return ::stat(map(p).c_str(), &st) == 0;
  }
  bool mkdir(const char* p, bool = true) {
    std::error_code ec;
    std::filesystem::create_directories(map(p), ec);
    return !ec && std::filesystem::is_directory(map(p), ec);
  }
  bool remove(const char* p) { return std::remove(map(p).c_str()) == 0; }
  bool rename(const char* a, const char* b) { return std::rename(map(a).c_str(), map(b).c_str()) == 0; }
  bool removeDir(const char* p) {
    std::error_code ec;
    std::filesystem::remove_all(map(p), ec);
    return !ec;
  }
};
#define Storage HalStorage::getInstance()
