#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>
#include <errno.h>
#include <unistd.h>

int main(void) {
    if (socket(AF_INET, SOCK_STREAM, 0) != -1 || errno != EACCES) return 1;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return 2;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(9);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != -1 || errno != EACCES) return 3;
    close(fd);
    struct addrinfo *result = 0;
    if (getaddrinfo("example.invalid", "443", 0, &result) != EAI_FAIL) return 4;
    return 0;
}
