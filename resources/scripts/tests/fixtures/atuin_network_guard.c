#include <sys/socket.h>
#include <netdb.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

__attribute__((constructor)) static void loaded(void) {
    const char *path = getenv("ENVY_GUARD_LOAD_AUDIT");
    if (!path) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) {
        const char *name = getprogname();
        write(fd, name, strlen(name));
        write(fd, "\n", 1);
        close(fd);
    }
}

static void audit(const char *event) {
    const char *path = getenv("ENVY_NETWORK_AUDIT");
    if (!path) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) {
        write(fd, event, strlen(event));
        close(fd);
    }
}

static int guarded_socket(int domain, int type, int protocol) {
    if (domain == AF_INET || domain == AF_INET6) {
        audit("socket\n");
        errno = EACCES;
        return -1;
    }
    return socket(domain, type, protocol);
}

static int guarded_connect(int fd, const struct sockaddr *address, socklen_t size) {
    if (address && (address->sa_family == AF_INET || address->sa_family == AF_INET6)) {
        audit("connect\n");
        errno = EACCES;
        return -1;
    }
    return connect(fd, address, size);
}

static int guarded_getaddrinfo(const char *node, const char *service,
                              const struct addrinfo *hints, struct addrinfo **result) {
    (void)node; (void)service; (void)hints; (void)result;
    audit("getaddrinfo\n");
    return EAI_FAIL;
}

#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_function; const void *old_function; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = \
    { (const void *)&replacement, (const void *)&original }

INTERPOSE(guarded_socket, socket);
INTERPOSE(guarded_connect, connect);
INTERPOSE(guarded_getaddrinfo, getaddrinfo);
