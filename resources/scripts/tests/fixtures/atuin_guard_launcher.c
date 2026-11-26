#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *library = getenv("ENVY_GUARD_LIBRARY");
    if (argc < 2 || !library) return 2;
    if (setenv("DYLD_INSERT_LIBRARIES", library, 1)) return 3;
    execv(argv[1], argv + 1);
    return 4;
}
