#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum {
    STONE_LOW_POWER_BRANCH_OFFSET = 0x33818,
    STONE_MIN_SIZE = 3 * 1024 * 1024,
    STONE_MAX_SIZE = 8 * 1024 * 1024
};

static const uint8_t elf_magic[4] = {0x7f, 'E', 'L', 'F'};
static const uint8_t low_power_branch[4] = {0x0a, 0x00, 0x40, 0x14};
static const uint8_t mips_nop[4] = {0x00, 0x00, 0x00, 0x00};

static int read_exact(int fd, off_t offset, void *buf, size_t len) {
    ssize_t got;

    if (lseek(fd, offset, SEEK_SET) < 0) {
        return -1;
    }

    got = read(fd, buf, len);
    if (got < 0) {
        return -1;
    }
    return (size_t)got == len ? 0 : -1;
}

static int validate_stone(int fd, const char *path, int verbose) {
    struct stat st;
    uint8_t buf[4];

    if (fstat(fd, &st) < 0) {
        if (verbose) {
            fprintf(stderr, "%s: stat failed: %s\n", path, strerror(errno));
        }
        return 1;
    }
    if (st.st_size < STONE_MIN_SIZE || st.st_size > STONE_MAX_SIZE) {
        if (verbose) {
            fprintf(stderr, "%s: size %ld outside expected stone-main range\n",
                    path, (long)st.st_size);
        }
        return 1;
    }

    if (read_exact(fd, 0, buf, sizeof(buf)) < 0) {
        if (verbose) {
            fprintf(stderr, "%s: cannot read ELF header: %s\n", path, strerror(errno));
        }
        return 1;
    }
    if (memcmp(buf, elf_magic, sizeof(buf)) != 0) {
        if (verbose) {
            fprintf(stderr, "%s: not an ELF executable\n", path);
        }
        return 1;
    }

    if (read_exact(fd, STONE_LOW_POWER_BRANCH_OFFSET, buf, sizeof(buf)) < 0) {
        if (verbose) {
            fprintf(stderr, "%s: cannot read patch offset 0x%x: %s\n",
                    path, STONE_LOW_POWER_BRANCH_OFFSET, strerror(errno));
        }
        return 1;
    }

    if (memcmp(buf, low_power_branch, sizeof(buf)) == 0) {
        if (verbose) {
            printf("%s: expected low-power branch found\n", path);
        }
        return 0;
    }

    if (memcmp(buf, mips_nop, sizeof(buf)) == 0) {
        if (verbose) {
            printf("%s: already patched\n", path);
        }
        return 0;
    }

    if (verbose) {
        fprintf(stderr, "%s: unexpected bytes at 0x%x: %02x%02x%02x%02x\n",
                path, STONE_LOW_POWER_BRANCH_OFFSET,
                buf[0], buf[1], buf[2], buf[3]);
    }
    return 1;
}

int main(int argc, char **argv) {
    const char *path;
    int check_only = 0;
    int fd;
    uint8_t buf[4];

    if (argc == 3 && strcmp(argv[1], "--check") == 0) {
        check_only = 1;
        path = argv[2];
    } else if (argc == 2) {
        path = argv[1];
    } else {
        fprintf(stderr, "usage: %s [--check] <stone-main>\n", argv[0]);
        return 2;
    }

    fd = open(path, check_only ? O_RDONLY : O_RDWR);
    if (fd < 0) {
        fprintf(stderr, "%s: open failed: %s\n", path, strerror(errno));
        return 1;
    }

    if (validate_stone(fd, path, 1) != 0) {
        close(fd);
        return 1;
    }

    if (check_only) {
        close(fd);
        return 0;
    }

    if (read_exact(fd, STONE_LOW_POWER_BRANCH_OFFSET, buf, sizeof(buf)) < 0) {
        fprintf(stderr, "%s: read before patch failed: %s\n", path, strerror(errno));
        close(fd);
        return 1;
    }

    if (memcmp(buf, mips_nop, sizeof(buf)) == 0) {
        close(fd);
        return 0;
    }

    if (lseek(fd, STONE_LOW_POWER_BRANCH_OFFSET, SEEK_SET) < 0) {
        fprintf(stderr, "%s: seek before patch failed: %s\n", path, strerror(errno));
        close(fd);
        return 1;
    }

    if (write(fd, mips_nop, sizeof(mips_nop)) != (ssize_t)sizeof(mips_nop)) {
        fprintf(stderr, "%s: patch write failed: %s\n", path, strerror(errno));
        close(fd);
        return 1;
    }

    fsync(fd);
    close(fd);
    printf("%s: patched low-power branch\n", path);
    return 0;
}
