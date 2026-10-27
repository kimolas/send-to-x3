#pragma once
// Host WiFiClient over real POSIX TCP sockets, so the firmware's own
// freeink::SecureHttpClient (plain-http path) talks to a real server and the exact
// bytes it writes are what the device would send. WireTrace keeps a copy.
#include <Client.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <string>

struct WireTrace {
  static std::string& sent() {
    static std::string s;
    return s;
  }
  static int& connects() {
    static int n = 0;
    return n;
  }
};

class WiFiClient : public Client {
 public:
  ~WiFiClient() override { stop(); }
  int connect(IPAddress, uint16_t) override { return 0; }
  int connect(const char* host, uint16_t port) override {
    stop();
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    char portStr[8];
    snprintf(portStr, sizeof(portStr), "%u", port);
    if (getaddrinfo(host, portStr, &hints, &res) != 0) return 0;
    for (addrinfo* a = res; a; a = a->ai_next) {
      fd_ = ::socket(a->ai_family, a->ai_socktype, a->ai_protocol);
      if (fd_ < 0) continue;
      if (::connect(fd_, a->ai_addr, a->ai_addrlen) == 0) break;
      ::close(fd_);
      fd_ = -1;
    }
    freeaddrinfo(res);
    if (fd_ < 0) return 0;
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(fd_, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
    fcntl(fd_, F_SETFL, fcntl(fd_, F_GETFL) | O_NONBLOCK);
    WireTrace::connects()++;
    return 1;
  }
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t size) override {
    if (fd_ < 0) return 0;
    size_t off = 0;
    while (off < size) {
#ifdef MSG_NOSIGNAL
      const ssize_t n = ::send(fd_, buf + off, size - off, MSG_NOSIGNAL);
#else
      const ssize_t n = ::send(fd_, buf + off, size - off, 0);
#endif
      if (n > 0) {
        off += static_cast<size_t>(n);
        continue;
      }
      if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        pollfd p{fd_, POLLOUT, 0};
        ::poll(&p, 1, 100);
        continue;
      }
      break;
    }
    WireTrace::sent().append(reinterpret_cast<const char*>(buf), off);
    return off;
  }
  int available() override {
    if (fd_ < 0) return 0;
    int n = 0;
    if (ioctl(fd_, FIONREAD, &n) < 0) return 0;
    return n;
  }
  int read() override {
    uint8_t c;
    return read(&c, 1) == 1 ? c : -1;
  }
  int read(uint8_t* buf, size_t size) override {
    if (fd_ < 0) return -1;
    const ssize_t n = ::recv(fd_, buf, size, 0);
    return n > 0 ? static_cast<int>(n) : -1;
  }
  int peek() override {
    if (fd_ < 0) return -1;
    uint8_t c;
    return ::recv(fd_, &c, 1, MSG_PEEK) == 1 ? c : -1;
  }
  void flush() override {}
  void stop() override {
    if (fd_ >= 0) ::close(fd_);
    fd_ = -1;
  }
  uint8_t connected() override {
    if (fd_ < 0) return 0;
    if (available() > 0) return 1;  // Arduino semantics: unread data keeps it "connected"
    uint8_t c;
    const ssize_t n = ::recv(fd_, &c, 1, MSG_PEEK);
    if (n == 0) return 0;  // orderly shutdown by the peer
    if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) return 0;
    return 1;
  }
  operator bool() override { return connected(); }
  void setConnectionTimeout(unsigned long) {}

 private:
  int fd_ = -1;
};
