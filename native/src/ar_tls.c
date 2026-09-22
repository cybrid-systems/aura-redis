/* P2.15 — optional native OpenSSL TLS (server accept on --tls-port). */
#include "ar_internal.h"

#include <stdio.h>
#include <string.h>

#ifdef AURA_REDIS_HAS_TLS
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <sys/epoll.h>
#endif

#ifndef AURA_REDIS_HAS_TLS

int ar_tls_available(void) { return 0; }

int ar_core_set_tls_cert_file(ArCore* core, const char* path) {
  (void)core;
  (void)path;
  return 0;
}
int ar_core_set_tls_key_file(ArCore* core, const char* path) {
  (void)core;
  (void)path;
  return 0;
}
int ar_core_set_tls_ca_file(ArCore* core, const char* path) {
  (void)core;
  (void)path;
  return 0;
}
const char* ar_core_tls_cert_file(ArCore* core) {
  (void)core;
  return "";
}
const char* ar_core_tls_key_file(ArCore* core) {
  (void)core;
  return "";
}
const char* ar_core_tls_ca_file(ArCore* core) {
  (void)core;
  return "";
}
int ar_core_tls_port(ArCore* core) {
  (void)core;
  return 0;
}
int ar_core_tls_enabled(ArCore* core) {
  (void)core;
  return 0;
}
void ar_tls_conn_free(ArConn* c) { (void)c; }
int ar_tls_handshake(ArCore* core, ArConn* c) {
  (void)core;
  (void)c;
  return -1;
}
ssize_t ar_tls_read(ArConn* c, void* buf, size_t n, int* want_write) {
  (void)c;
  (void)buf;
  (void)n;
  if (want_write)
    *want_write = 0;
  return -1;
}
ssize_t ar_tls_write(ArConn* c, const void* buf, size_t n, int* want_read) {
  (void)c;
  (void)buf;
  (void)n;
  if (want_read)
    *want_read = 0;
  return -1;
}
int ar_tls_accept_setup(ArCore* core, ArConn* c) {
  (void)core;
  (void)c;
  return -1;
}
void ar_tls_free_ctx(ArCore* core) { (void)core; }
int ar_tls_setup_ctx(ArCore* core) {
  (void)core;
  return 0;
}

#else /* AURA_REDIS_HAS_TLS */

int ar_tls_available(void) { return 1; }

static int copy_path(char* dst, size_t dstsz, const char* path) {
  if (!path || !path[0] || strlen(path) >= dstsz)
    return 0;
  snprintf(dst, dstsz, "%s", path);
  return 1;
}

int ar_core_set_tls_cert_file(ArCore* core, const char* path) {
  if (!core)
    return 0;
  return copy_path(core->tls_cert_file, sizeof(core->tls_cert_file), path);
}
int ar_core_set_tls_key_file(ArCore* core, const char* path) {
  if (!core)
    return 0;
  return copy_path(core->tls_key_file, sizeof(core->tls_key_file), path);
}
int ar_core_set_tls_ca_file(ArCore* core, const char* path) {
  if (!core)
    return 0;
  if (!path || !path[0]) {
    core->tls_ca_file[0] = '\0';
    return 1;
  }
  return copy_path(core->tls_ca_file, sizeof(core->tls_ca_file), path);
}
const char* ar_core_tls_cert_file(ArCore* core) {
  return core && core->tls_cert_file[0] ? core->tls_cert_file : "";
}
const char* ar_core_tls_key_file(ArCore* core) {
  return core && core->tls_key_file[0] ? core->tls_key_file : "";
}
const char* ar_core_tls_ca_file(ArCore* core) {
  return core && core->tls_ca_file[0] ? core->tls_ca_file : "";
}
int ar_core_tls_port(ArCore* core) { return core ? core->tls_port : 0; }
int ar_core_tls_enabled(ArCore* core) {
  return core && core->tls_listen_fd >= 0 ? 1 : 0;
}

static int ensure_ssl_lib(void) {
  static int once = 0;
  if (!once) {
    SSL_library_init();
    SSL_load_error_strings();
    OpenSSL_add_all_algorithms();
    once = 1;
  }
  return 1;
}

int ar_tls_setup_ctx(ArCore* core) {
  if (!ensure_ssl_lib())
    return 0;
  if (core->ssl_ctx)
    return 1;
  if (!core->tls_cert_file[0] || !core->tls_key_file[0]) {
    fprintf(stderr, "aura-redis: TLS needs --tls-cert-file and --tls-key-file\n");
    return 0;
  }
  SSL_CTX* ctx = SSL_CTX_new(TLS_server_method());
  if (!ctx) {
    fprintf(stderr, "aura-redis: SSL_CTX_new failed\n");
    return 0;
  }
  SSL_CTX_set_min_proto_version(ctx, TLS1_2_VERSION);
  if (SSL_CTX_use_certificate_file(ctx, core->tls_cert_file, SSL_FILETYPE_PEM) !=
      1) {
    fprintf(stderr, "aura-redis: bad --tls-cert-file %s\n", core->tls_cert_file);
    SSL_CTX_free(ctx);
    return 0;
  }
  if (SSL_CTX_use_PrivateKey_file(ctx, core->tls_key_file, SSL_FILETYPE_PEM) !=
      1) {
    fprintf(stderr, "aura-redis: bad --tls-key-file %s\n", core->tls_key_file);
    SSL_CTX_free(ctx);
    return 0;
  }
  if (SSL_CTX_check_private_key(ctx) != 1) {
    fprintf(stderr, "aura-redis: TLS cert/key mismatch\n");
    SSL_CTX_free(ctx);
    return 0;
  }
  if (core->tls_ca_file[0]) {
    if (SSL_CTX_load_verify_locations(ctx, core->tls_ca_file, NULL) != 1) {
      fprintf(stderr, "aura-redis: bad --tls-ca-file %s\n", core->tls_ca_file);
      SSL_CTX_free(ctx);
      return 0;
    }
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
                       NULL);
  } else {
    SSL_CTX_set_verify(ctx, SSL_VERIFY_NONE, NULL);
  }
  core->ssl_ctx = ctx;
  return 1;
}

void ar_tls_free_ctx(ArCore* core) {
  if (!core || !core->ssl_ctx)
    return;
  SSL_CTX_free((SSL_CTX*)core->ssl_ctx);
  core->ssl_ctx = NULL;
}

int ar_tls_accept_setup(ArCore* core, ArConn* c) {
  if (!core || !c || !core->ssl_ctx || c->fd < 0)
    return -1;
  SSL* ssl = SSL_new((SSL_CTX*)core->ssl_ctx);
  if (!ssl)
    return -1;
  if (SSL_set_fd(ssl, c->fd) != 1) {
    SSL_free(ssl);
    return -1;
  }
  SSL_set_accept_state(ssl);
  c->ssl = ssl;
  c->is_tls = 1;
  c->ssl_hs_done = 0;
  return 0;
}

void ar_tls_conn_free(ArConn* c) {
  if (!c || !c->ssl)
    return;
  SSL* ssl = (SSL*)c->ssl;
  /* Non-blocking best-effort; ignore return. */
  SSL_shutdown(ssl);
  SSL_free(ssl);
  c->ssl = NULL;
  c->is_tls = 0;
  c->ssl_hs_done = 0;
}

static void arm_epoll(ArCore* core, ArConn* c, int want_write) {
  struct epoll_event ev;
  ev.events = EPOLLIN | EPOLLET;
  if (want_write)
    ev.events |= EPOLLOUT;
  ev.data.ptr = c;
  epoll_ctl(core->epfd, EPOLL_CTL_MOD, c->fd, &ev);
  c->want_write = want_write ? 1 : 0;
}

int ar_tls_handshake(ArCore* core, ArConn* c) {
  if (!c || !c->ssl)
    return -1;
  if (c->ssl_hs_done)
    return 1;
  SSL* ssl = (SSL*)c->ssl;
  int r = SSL_accept(ssl);
  if (r == 1) {
    c->ssl_hs_done = 1;
    arm_epoll(core, c, 0);
    return 1;
  }
  int err = SSL_get_error(ssl, r);
  if (err == SSL_ERROR_WANT_READ) {
    arm_epoll(core, c, 0);
    return 0;
  }
  if (err == SSL_ERROR_WANT_WRITE) {
    arm_epoll(core, c, 1);
    return 0;
  }
  return -1;
}

ssize_t ar_tls_read(ArConn* c, void* buf, size_t n, int* want_write) {
  if (want_write)
    *want_write = 0;
  if (!c || !c->ssl || !c->ssl_hs_done)
    return -1;
  int r = SSL_read((SSL*)c->ssl, buf, (int)n);
  if (r > 0)
    return r;
  int err = SSL_get_error((SSL*)c->ssl, r);
  if (err == SSL_ERROR_WANT_READ)
    return 0; /* EAGAIN */
  if (err == SSL_ERROR_WANT_WRITE) {
    if (want_write)
      *want_write = 1;
    return 0;
  }
  if (err == SSL_ERROR_ZERO_RETURN || r == 0)
    return -2; /* peer closed */
  return -1;
}

ssize_t ar_tls_write(ArConn* c, const void* buf, size_t n, int* want_read) {
  if (want_read)
    *want_read = 0;
  if (!c || !c->ssl || !c->ssl_hs_done)
    return -1;
  int r = SSL_write((SSL*)c->ssl, buf, (int)n);
  if (r > 0)
    return r;
  int err = SSL_get_error((SSL*)c->ssl, r);
  if (err == SSL_ERROR_WANT_WRITE)
    return 0; /* EAGAIN */
  if (err == SSL_ERROR_WANT_READ) {
    if (want_read)
      *want_read = 1;
    return 0;
  }
  return -1;
}

#endif /* AURA_REDIS_HAS_TLS */
