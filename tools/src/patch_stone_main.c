#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum {
    STONE_MIN_SIZE = 3 * 1024 * 1024,
    STONE_MAX_SIZE = 8 * 1024 * 1024,
    ELF32_EHDR_SIZE = 52,
    ELF32_PHDR_SIZE = 32,
    ELFCLASS32_VALUE = 1,
    ELFDATA2LSB_VALUE = 1,
    EM_MIPS_VALUE = 8,
    PT_LOAD_VALUE = 1,
    SNAPSHOT_PATCH_WORDS = 14
};

typedef struct {
    uint32_t value;
    uint32_t mask;
} WordPattern;

typedef struct {
    off_t low_power_offset;
    off_t snapshot_offset;
    uint32_t snapshot_target_vaddr;
    uint32_t low_power_word;
    int snapshot_is_patched;
    uint8_t snapshot_patch[SNAPSHOT_PATCH_WORDS * 4];
} PatchLayout;

static const uint8_t elf_magic[4] = {0x7f, 'E', 'L', 'F'};

/*
 * This is the control-flow neighborhood around the stock low-power decision.
 * Absolute call targets, global-data offsets, and branch displacements are
 * deliberately masked. The actual patch site is word 6.
 */
static const WordPattern low_power_context[] = {
    {0x24430001U, 0xffffffffU}, /* addiu v1,v0,1 */
    {0x28420005U, 0xffffffffU}, /* slti v0,v0,5 */
    {0x14400000U, 0xffff0000U}, /* bne v0,zero,... */
    {0xaea30000U, 0xffff0000U}, /* sw v1,...(s5) */
    {0x0c000000U, 0xfc000000U}, /* jal ... */
    {0x00000000U, 0xffffffffU}, /* nop */
    {0x00000000U, 0x00000000U}, /* low-power branch or patched nop */
    {0x8fa20018U, 0xffffffffU}, /* lw v0,0x18(sp) */
    {0x0c000000U, 0xfc000000U}, /* jal ... */
    {0x24040001U, 0xffffffffU}, /* li a0,1 */
    {0x82020043U, 0xffffffffU}, /* lb v0,0x43(s0) */
    {0x14510000U, 0xffff0000U}, /* bne v0,s1,... */
    {0x00000000U, 0xffffffffU}, /* nop */
    {0x0c000000U, 0xfc000000U}, /* jal ... */
    {0x24040005U, 0xffffffffU}, /* li a0,5 */
};

/* The misc_save_pic callback prologue. Call and string addresses may move. */
static const WordPattern snapshot_callback_context[] = {
    {0x27bdffd8U, 0xffffffffU}, /* addiu sp,sp,-0x28 */
    {0xafb0001cU, 0xffffffffU}, /* sw s0,0x1c(sp) */
    {0x8c900010U, 0xffffffffU}, /* lw s0,0x10(a0) */
    {0xafb10020U, 0xffffffffU}, /* sw s1,0x20(sp) */
    {0xafbf0024U, 0xffffffffU}, /* sw ra,0x24(sp) */
    {0x26110014U, 0xffffffffU}, /* addiu s1,s0,0x14 */
    {0x0c000000U, 0xfc000000U}, /* jal strlen */
    {0x02202025U, 0xffffffffU}, /* move a0,s1 */
    {0x2c420033U, 0xffffffffU}, /* sltiu v0,v0,0x33 */
    {0x10400000U, 0xffff0000U}, /* beq v0,zero,... */
    {0x02202825U, 0xffffffffU}, /* move a1,s1 */
    {0x8e060010U, 0xffffffffU}, /* lw a2,0x10(s0) */
    {0x3c040000U, 0xffff0000U}, /* lui a0,... */
    {0x0c000000U, 0xfc000000U}, /* jal printf */
};

/* Stable prologue of msnapshot_get_file in both known firmware builds. */
static const WordPattern snapshot_target_context[] = {
    {0x27bdffb0U, 0xffffffffU},
    {0xafbf004cU, 0xffffffffU},
    {0xafbe0048U, 0xffffffffU},
    {0x03a0f025U, 0xffffffffU},
    {0xafc40050U, 0xffffffffU},
    {0xafc50054U, 0xffffffffU},
    {0xafc60058U, 0xffffffffU},
    {0xafc7005cU, 0xffffffffU},
    {0x2402ffffU, 0xffffffffU},
    {0xafc20020U, 0xffffffffU},
    {0x24020320U, 0xffffffffU},
    {0xafc20028U, 0xffffffffU},
};

static uint16_t read_u16_le(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t read_u32_le(const uint8_t *p) {
    return (uint32_t)p[0] |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void write_u32_le(uint8_t *p, uint32_t value) {
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16);
    p[3] = (uint8_t)(value >> 24);
}

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

static int load_file(int fd, const char *path, uint8_t **data_out, size_t *size_out) {
    struct stat st;
    uint8_t *data;

    if (fstat(fd, &st) < 0) {
        fprintf(stderr, "%s: stat failed: %s\n", path, strerror(errno));
        return 1;
    }
    if (st.st_size < STONE_MIN_SIZE || st.st_size > STONE_MAX_SIZE) {
        fprintf(stderr, "%s: size %ld outside expected stone-main range\n",
                path, (long)st.st_size);
        return 1;
    }

    data = (uint8_t *)malloc((size_t)st.st_size);
    if (data == NULL) {
        fprintf(stderr, "%s: cannot allocate %ld bytes\n", path, (long)st.st_size);
        return 1;
    }
    if (read_exact(fd, 0, data, (size_t)st.st_size) < 0) {
        fprintf(stderr, "%s: read failed: %s\n", path, strerror(errno));
        free(data);
        return 1;
    }

    *data_out = data;
    *size_out = (size_t)st.st_size;
    return 0;
}

static int validate_elf32_mips(const uint8_t *data, size_t size, const char *path) {
    if (size < ELF32_EHDR_SIZE || memcmp(data, elf_magic, sizeof(elf_magic)) != 0) {
        fprintf(stderr, "%s: not an ELF executable\n", path);
        return 1;
    }
    if (data[4] != ELFCLASS32_VALUE || data[5] != ELFDATA2LSB_VALUE ||
        read_u16_le(data + 18) != EM_MIPS_VALUE) {
        fprintf(stderr, "%s: expected a 32-bit little-endian MIPS ELF\n", path);
        return 1;
    }
    return 0;
}

static int file_offset_to_vaddr(const uint8_t *data, size_t size, off_t file_offset,
                                uint32_t *vaddr_out) {
    uint32_t phoff;
    uint16_t phentsize;
    uint16_t phnum;
    uint16_t i;

    phoff = read_u32_le(data + 28);
    phentsize = read_u16_le(data + 42);
    phnum = read_u16_le(data + 44);
    if (phentsize < ELF32_PHDR_SIZE || phoff > size ||
        phnum > (size - phoff) / phentsize) {
        return 1;
    }

    for (i = 0; i < phnum; i++) {
        const uint8_t *ph = data + phoff + ((size_t)i * phentsize);
        uint32_t type = read_u32_le(ph);
        uint32_t offset = read_u32_le(ph + 4);
        uint32_t vaddr = read_u32_le(ph + 8);
        uint32_t filesz = read_u32_le(ph + 16);
        uint64_t end = (uint64_t)offset + filesz;

        if (type == PT_LOAD_VALUE && (uint64_t)file_offset >= offset &&
            (uint64_t)file_offset < end) {
            uint64_t mapped = (uint64_t)vaddr + ((uint64_t)file_offset - offset);
            if (mapped > 0xffffffffU) {
                return 1;
            }
            *vaddr_out = (uint32_t)mapped;
            return 0;
        }
    }
    return 1;
}

static size_t count_word_pattern(const uint8_t *data, size_t size,
                                 const WordPattern *pattern, size_t word_count,
                                 off_t *first_offset) {
    size_t off;
    size_t count = 0;
    size_t byte_count = word_count * 4;

    if (byte_count > size) {
        return 0;
    }
    for (off = 0; off <= size - byte_count; off += 4) {
        size_t i;
        for (i = 0; i < word_count; i++) {
            uint32_t word = read_u32_le(data + off + (i * 4));
            if ((word & pattern[i].mask) != (pattern[i].value & pattern[i].mask)) {
                break;
            }
        }
        if (i == word_count) {
            if (count == 0 && first_offset != NULL) {
                *first_offset = (off_t)off;
            }
            count++;
        }
    }
    return count;
}

static int find_unique_word_pattern(const uint8_t *data, size_t size,
                                    const WordPattern *pattern, size_t word_count,
                                    const char *path, const char *label, off_t *offset_out) {
    size_t count = count_word_pattern(data, size, pattern, word_count, offset_out);

    if (count != 1) {
        fprintf(stderr, "%s: expected one %s signature, found %lu\n",
                path, label, (unsigned long)count);
        return 1;
    }
    return 0;
}

static uint32_t make_jal(uint32_t pc, uint32_t target) {
    if (((pc + 4) & 0xf0000000U) != (target & 0xf0000000U) || (target & 3U) != 0) {
        return 0;
    }
    return 0x0c000000U | ((target >> 2) & 0x03ffffffU);
}

static int build_snapshot_patch(uint8_t *patch, uint32_t callback_vaddr,
                                uint32_t target_vaddr) {
    static const uint32_t words[SNAPSHOT_PATCH_WORDS] = {
        0x27bdffe0U, /* addiu sp,sp,-0x20 */
        0xafbf001cU, /* sw ra,0x1c(sp) */
        0x8c820010U, /* lw v0,0x10(a0) */
        0x24040001U, /* li a0,1 */
        0x24450014U, /* addiu a1,v0,0x14 */
        0x00003025U, /* move a2,zero */
        0x00003825U, /* move a3,zero */
        0xafa00010U, /* sw zero,0x10(sp) */
        0x00000000U, /* generated jal msnapshot_get_file */
        0x00000000U, /* nop */
        0x8fbf001cU, /* lw ra,0x1c(sp) */
        0x27bd0020U, /* addiu sp,sp,0x20 */
        0x03e00008U, /* jr ra */
        0x00000000U, /* nop */
    };
    uint32_t jal;
    size_t i;

    jal = make_jal(callback_vaddr + (8 * 4), target_vaddr);
    if (jal == 0) {
        return 1;
    }
    for (i = 0; i < SNAPSHOT_PATCH_WORDS; i++) {
        write_u32_le(patch + (i * 4), i == 8 ? jal : words[i]);
    }
    return 0;
}

static int is_low_power_branch(uint32_t word) {
    return (word & 0xffff0000U) == 0x14400000U;
}

static int analyze_layout(const uint8_t *data, size_t size, const char *path,
                          PatchLayout *layout) {
    off_t low_context_offset;
    off_t original_snapshot_offset = 0;
    off_t target_offset;
    uint32_t callback_vaddr;
    size_t original_count;

    if (validate_elf32_mips(data, size, path) != 0) {
        return 1;
    }
    if (find_unique_word_pattern(data, size, low_power_context,
                                 sizeof(low_power_context) / sizeof(low_power_context[0]),
                                 path, "low-power control-flow", &low_context_offset) != 0) {
        return 1;
    }
    layout->low_power_offset = low_context_offset + (6 * 4);
    layout->low_power_word = read_u32_le(data + layout->low_power_offset);
    if (layout->low_power_word != 0 && !is_low_power_branch(layout->low_power_word)) {
        fprintf(stderr, "%s: unexpected low-power instruction 0x%08x at 0x%lx\n",
                path, layout->low_power_word, (long)layout->low_power_offset);
        return 1;
    }

    if (find_unique_word_pattern(data, size, snapshot_target_context,
                                 sizeof(snapshot_target_context) /
                                     sizeof(snapshot_target_context[0]),
                                 path, "msnapshot_get_file", &target_offset) != 0) {
        return 1;
    }
    if (file_offset_to_vaddr(data, size, target_offset,
                             &layout->snapshot_target_vaddr) != 0) {
        fprintf(stderr, "%s: cannot map snapshot target offset 0x%lx to ELF address\n",
                path, (long)target_offset);
        return 1;
    }

    original_count = count_word_pattern(
        data, size, snapshot_callback_context,
        sizeof(snapshot_callback_context) / sizeof(snapshot_callback_context[0]),
        &original_snapshot_offset);
    if (original_count > 1) {
        fprintf(stderr, "%s: expected at most one stock snapshot callback, found %lu\n",
                path, (unsigned long)original_count);
        return 1;
    }

    if (original_count == 1) {
        layout->snapshot_offset = original_snapshot_offset;
        if (file_offset_to_vaddr(data, size, layout->snapshot_offset, &callback_vaddr) != 0 ||
            build_snapshot_patch(layout->snapshot_patch, callback_vaddr,
                                 layout->snapshot_target_vaddr) != 0) {
            fprintf(stderr, "%s: cannot build snapshot callback patch\n", path);
            return 1;
        }
        layout->snapshot_is_patched = 0;
        return 0;
    }

    /* Find the generated patch when the stock prologue has already been replaced. */
    {
        off_t candidate;
        size_t off;
        size_t found = 0;

        for (off = 0; off + sizeof(layout->snapshot_patch) <= size; off += 4) {
            if (read_u32_le(data + off) != 0x27bdffe0U ||
                read_u32_le(data + off + 4) != 0xafbf001cU) {
                continue;
            }
            candidate = (off_t)off;
            if (file_offset_to_vaddr(data, size, candidate, &callback_vaddr) != 0 ||
                build_snapshot_patch(layout->snapshot_patch, callback_vaddr,
                                     layout->snapshot_target_vaddr) != 0) {
                continue;
            }
            if (memcmp(data + off, layout->snapshot_patch,
                       sizeof(layout->snapshot_patch)) == 0) {
                layout->snapshot_offset = candidate;
                found++;
            }
        }
        if (found != 1) {
            fprintf(stderr, "%s: expected one stock or patched snapshot callback, found %lu\n",
                    path, (unsigned long)found);
            return 1;
        }
        if (file_offset_to_vaddr(data, size, layout->snapshot_offset,
                                 &callback_vaddr) != 0 ||
            build_snapshot_patch(layout->snapshot_patch, callback_vaddr,
                                 layout->snapshot_target_vaddr) != 0) {
            fprintf(stderr, "%s: cannot rebuild snapshot callback patch\n", path);
            return 1;
        }
    }
    layout->snapshot_is_patched = 1;
    return 0;
}

static int apply_layout(int fd, const char *path, const PatchLayout *layout,
                        int keep_low_power, int *changed) {
    uint8_t word_bytes[4];
    uint32_t target_low_power;

    target_low_power = keep_low_power ? layout->low_power_word : 0;
    if (layout->low_power_word != target_low_power) {
        write_u32_le(word_bytes, target_low_power);
        if (write_exact(fd, layout->low_power_offset, word_bytes, sizeof(word_bytes)) < 0) {
            fprintf(stderr, "%s: low-power patch write failed: %s\n",
                    path, strerror(errno));
            return 1;
        }
        *changed = 1;
        printf("%s: updated low-power branch at 0x%lx\n",
               path, (long)layout->low_power_offset);
    }

    if (!layout->snapshot_is_patched) {
        if (write_exact(fd, layout->snapshot_offset, layout->snapshot_patch,
                        sizeof(layout->snapshot_patch)) < 0) {
            fprintf(stderr, "%s: snapshot patch write failed: %s\n",
                    path, strerror(errno));
            return 1;
        }
        *changed = 1;
        printf("%s: updated snapshot callback at 0x%lx (target 0x%08x)\n",
               path, (long)layout->snapshot_offset, layout->snapshot_target_vaddr);
    }
    return 0;
}

int main(int argc, char **argv) {
    const char *path = NULL;
    uint8_t *data = NULL;
    size_t size = 0;
    PatchLayout layout;
    int check_only = 0;
    int keep_low_power = 0;
    int changed = 0;
    int fd;
    int i;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--check") == 0) {
            check_only = 1;
        } else if (strcmp(argv[i], "--keep-low-power") == 0) {
            keep_low_power = 1;
        } else if (path == NULL) {
            path = argv[i];
        } else {
            fprintf(stderr, "usage: %s [--check] [--keep-low-power] <stone-main>\n",
                    argv[0]);
            return 2;
        }
    }
    if (path == NULL) {
        fprintf(stderr, "usage: %s [--check] [--keep-low-power] <stone-main>\n",
                argv[0]);
        return 2;
    }

    fd = open(path, check_only ? O_RDONLY : O_RDWR);
    if (fd < 0) {
        fprintf(stderr, "%s: open failed: %s\n", path, strerror(errno));
        return 1;
    }
    if (load_file(fd, path, &data, &size) != 0) {
        close(fd);
        return 1;
    }
    memset(&layout, 0, sizeof(layout));
    if (analyze_layout(data, size, path, &layout) != 0) {
        free(data);
        close(fd);
        return 1;
    }
    if (keep_low_power && layout.low_power_word == 0) {
        fprintf(stderr,
                "%s: cannot reconstruct the stock low-power branch; recopy the stock executable\n",
                path);
        free(data);
        close(fd);
        return 1;
    }

    printf("%s: discovered low-power branch at 0x%lx (%s)\n",
           path, (long)layout.low_power_offset,
           layout.low_power_word == 0 ? "disabled" : "enabled");
    printf("%s: discovered snapshot callback at 0x%lx (%s), target 0x%08x\n",
           path, (long)layout.snapshot_offset,
           layout.snapshot_is_patched ? "patched" : "stock",
           layout.snapshot_target_vaddr);

    if (!check_only && apply_layout(fd, path, &layout, keep_low_power, &changed) != 0) {
        free(data);
        close(fd);
        return 1;
    }
    if (changed && fsync(fd) < 0) {
        fprintf(stderr, "%s: fsync failed: %s\n", path, strerror(errno));
        free(data);
        close(fd);
        return 1;
    }

    free(data);
    close(fd);
    return 0;
}
