#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum {
    STONE_LOW_POWER_BRANCH_OFFSET = 0x33818,
    STONE_SNAPSHOT_CALLBACK_OFFSET = 0x7a3cc,
    STONE_MIN_SIZE = 3 * 1024 * 1024,
    STONE_MAX_SIZE = 8 * 1024 * 1024
};

static const uint8_t elf_magic[4] = {0x7f, 'E', 'L', 'F'};
static const uint8_t low_power_branch[4] = {0x0a, 0x00, 0x40, 0x14};
static const uint8_t mips_nop[4] = {0x00, 0x00, 0x00, 0x00};
static const uint8_t snapshot_callback_original[] = {
    0xd8, 0xff, 0xbd, 0x27, 0x1c, 0x00, 0xb0, 0xaf,
    0x10, 0x00, 0x90, 0x8c, 0x20, 0x00, 0xb1, 0xaf,
    0x24, 0x00, 0xbf, 0xaf, 0x14, 0x00, 0x11, 0x26,
    0x70, 0xc3, 0x21, 0x0c, 0x25, 0x20, 0x20, 0x02,
    0x33, 0x00, 0x42, 0x2c, 0x42, 0x00, 0x40, 0x10,
    0x25, 0x28, 0x20, 0x02, 0x10, 0x00, 0x06, 0x8e,
    0x7b, 0x00, 0x04, 0x3c, 0x7c, 0xc3, 0x21, 0x0c,
};
static const uint8_t snapshot_callback_patch[] = {
    0xe0, 0xff, 0xbd, 0x27, 0x1c, 0x00, 0xbf, 0xaf,
    0x10, 0x00, 0x82, 0x8c, 0x01, 0x00, 0x04, 0x24,
    0x14, 0x00, 0x45, 0x24, 0x25, 0x30, 0x00, 0x00,
    0x25, 0x38, 0x00, 0x00, 0x10, 0x00, 0xa0, 0xaf,
    0xf4, 0xdb, 0x13, 0x0c, 0x00, 0x00, 0x00, 0x00,
    0x1c, 0x00, 0xbf, 0x8f, 0x20, 0x00, 0xbd, 0x27,
    0x08, 0x00, 0xe0, 0x03, 0x00, 0x00, 0x00, 0x00,
};
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

static int write_exact(int fd, off_t offset, const void *buf, size_t len) {
    ssize_t wrote;

    if (lseek(fd, offset, SEEK_SET) < 0) {
        return -1;
    }

    wrote = write(fd, buf, len);
    if (wrote < 0) {
        return -1;
    }
    return (size_t)wrote == len ? 0 : -1;
}

static void print_bytes(const uint8_t *buf, size_t len) {
    size_t i;

    for (i = 0; i < len; i++) {
        fprintf(stderr, "%02x", buf[i]);
    }
}

static int set_patch_site(int fd, const char *path, off_t offset, const char *label,
                      const uint8_t *from, const uint8_t *target,
                      size_t len, int apply, int *changed) {
    uint8_t buf[sizeof(snapshot_callback_original)];

    if (len > sizeof(buf)) {
        fprintf(stderr, "%s: patch site %s is too large\n", path, label);
        return 1;
    }

    if (read_exact(fd, offset, buf, len) < 0) {
        fprintf(stderr, "%s: cannot read %s offset 0x%lx: %s\n",
                path, label, (long)offset, strerror(errno));
        return 1;
    }

    if (memcmp(buf, target, len) == 0) {
        if (!apply) {
            printf("%s: %s already in target state\n", path, label);
        }
        return 0;
    }

    if (memcmp(buf, from, len) != 0) {
        if (apply) {
            fprintf(stderr, "%s: %s bytes changed after validation\n", path, label);
        } else {
            fprintf(stderr, "%s: unexpected %s bytes at 0x%lx: ", path, label, (long)offset);
            print_bytes(buf, len);
            fprintf(stderr, "\n");
        }
        return 1;
    }

    if (!apply) {
        printf("%s: expected %s bytes found\n", path, label);
        return 0;
    }

    if (write_exact(fd, offset, target, len) < 0) {
        fprintf(stderr, "%s: %s patch write failed: %s\n", path, label, strerror(errno));
        return 1;
    }
    if (changed != NULL) {
        *changed = 1;
    }
    printf("%s: updated %s\n", path, label);
    return 0;
}

static int check_patch_sites(int fd, const char *path, int keep_low_power) {
    if (keep_low_power) {
        if (set_patch_site(fd, path, STONE_LOW_POWER_BRANCH_OFFSET, "low-power branch",
                           mips_nop, low_power_branch, sizeof(low_power_branch), 0, NULL) != 0) {
            return 1;
        }
    } else if (set_patch_site(fd, path, STONE_LOW_POWER_BRANCH_OFFSET, "low-power branch",
                              low_power_branch, mips_nop, sizeof(low_power_branch), 0, NULL) != 0) {
        return 1;
    }
    if (set_patch_site(fd, path, STONE_SNAPSHOT_CALLBACK_OFFSET,
                       "ONVIF JPEG snapshot callback", snapshot_callback_original,
                       snapshot_callback_patch, sizeof(snapshot_callback_original), 0, NULL) != 0) {
        return 1;
    }
    return 0;
}

static int apply_patch_sites(int fd, const char *path, int keep_low_power, int *changed) {
    if (keep_low_power) {
        if (set_patch_site(fd, path, STONE_LOW_POWER_BRANCH_OFFSET, "low-power branch",
                           mips_nop, low_power_branch, sizeof(low_power_branch), 1, changed) != 0) {
            return 1;
        }
    } else if (set_patch_site(fd, path, STONE_LOW_POWER_BRANCH_OFFSET, "low-power branch",
                              low_power_branch, mips_nop, sizeof(low_power_branch), 1, changed) != 0) {
        return 1;
    }
    if (set_patch_site(fd, path, STONE_SNAPSHOT_CALLBACK_OFFSET,
                       "ONVIF JPEG snapshot callback", snapshot_callback_original,
                       snapshot_callback_patch, sizeof(snapshot_callback_original), 1, changed) != 0) {
        return 1;
    }
    return 0;
}

static int validate_stone(int fd, const char *path, int keep_low_power) {
    struct stat st;
    uint8_t buf[4];

    if (fstat(fd, &st) < 0) {
        fprintf(stderr, "%s: stat failed: %s\n", path, strerror(errno));
        return 1;
    }
    if (st.st_size < STONE_MIN_SIZE || st.st_size > STONE_MAX_SIZE) {
        fprintf(stderr, "%s: size %ld outside expected stone-main range\n",
                path, (long)st.st_size);
        return 1;
    }

    if (read_exact(fd, 0, buf, sizeof(buf)) < 0) {
        fprintf(stderr, "%s: cannot read ELF header: %s\n", path, strerror(errno));
        return 1;
    }
    if (memcmp(buf, elf_magic, sizeof(buf)) != 0) {
        fprintf(stderr, "%s: not an ELF executable\n", path);
        return 1;
    }

    if (check_patch_sites(fd, path, keep_low_power) != 0) {
        return 1;
    }

    return 0;
}

int main(int argc, char **argv) {
    const char *path;
    int check_only = 0;
    int keep_low_power = 0;
    int changed = 0;
    int fd;
    int i;

    path = NULL;
    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--check") == 0) {
            check_only = 1;
        } else if (strcmp(argv[i], "--keep-low-power") == 0) {
            keep_low_power = 1;
        } else if (path == NULL) {
            path = argv[i];
        } else {
            fprintf(stderr, "usage: %s [--check] [--keep-low-power] <stone-main>\n", argv[0]);
            return 2;
        }
    }

    if (path == NULL) {
        fprintf(stderr, "usage: %s [--check] [--keep-low-power] <stone-main>\n", argv[0]);
        return 2;
    }

    fd = open(path, check_only ? O_RDONLY : O_RDWR);
    if (fd < 0) {
        fprintf(stderr, "%s: open failed: %s\n", path, strerror(errno));
        return 1;
    }

    if (validate_stone(fd, path, keep_low_power) != 0) {
        close(fd);
        return 1;
    }

    if (check_only) {
        close(fd);
        return 0;
    }

    if (apply_patch_sites(fd, path, keep_low_power, &changed) != 0) {
        close(fd);
        return 1;
    }

    if (changed) {
        fsync(fd);
    }
    close(fd);
    return 0;
}
