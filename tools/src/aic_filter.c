#include <arpa/inet.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <net/if.h>

#ifndef SIOCDEVPRIVATE
#define SIOCDEVPRIVATE 0x89f0
#endif

#define WLC_SET_VAR 0x107
#define FILTER_IFACE "wlan0"
#define TELNET_FILTER_ID 0x67
#define STREAM_FILTER_ID 0x68
#define RTSP_FILTER_ID 0x69
#define ONVIF_FILTER_ID 0x6a
#define WSD_FILTER_ID 0x6b
#define TELNET_PORT 2323
#define STREAM_RELAY_PORT 8555
#define RTSP_PORT 8554
#define ONVIF_HTTP_PORT 8899
#define WSD_PORT 3702

struct wl_ioctl {
    uint32_t cmd;
    void *buf;
    uint32_t len;
    uint32_t set;
};

static void put_le16(unsigned char *p, uint16_t v) {
    p[0] = (unsigned char)(v & 0xff);
    p[1] = (unsigned char)((v >> 8) & 0xff);
}

static void put_le32(unsigned char *p, uint32_t v) {
    p[0] = (unsigned char)(v & 0xff);
    p[1] = (unsigned char)((v >> 8) & 0xff);
    p[2] = (unsigned char)((v >> 16) & 0xff);
    p[3] = (unsigned char)((v >> 24) & 0xff);
}

static int wl_ioctl_call(uint32_t cmd, void *buf, uint32_t len) {
    int fd;
    int ret;
    struct ifreq ifr;
    struct wl_ioctl ioc;

    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        fprintf(stderr, "socket failed: %s\n", strerror(errno));
        return -1;
    }

    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, FILTER_IFACE, IFNAMSIZ - 1);

    memset(&ioc, 0, sizeof(ioc));
    ioc.cmd = cmd;
    ioc.buf = buf;
    ioc.len = len;
    ioc.set = 1;
    ifr.ifr_data = (void *)&ioc;

    ret = ioctl(fd, SIOCDEVPRIVATE, &ifr);
    if (ret < 0) {
        fprintf(stderr, "ioctl(%s, 0x%x) failed: %s\n", FILTER_IFACE, SIOCDEVPRIVATE,
                strerror(errno));
    }
    close(fd);
    return ret;
}

static int set_iovar(const char *name, const void *params, size_t params_len) {
    unsigned char buf[512];
    size_t name_len = strlen(name) + 1;
    size_t len = name_len + params_len;
    size_t send_len;

    if (len > sizeof(buf)) {
        fprintf(stderr, "iovar buffer too small for %s\n", name);
        return -1;
    }

    memset(buf, 0, sizeof(buf));
    memcpy(buf, name, name_len);
    if (params_len != 0) {
        memcpy(buf + name_len, params, params_len);
    }

    send_len = len < 32 ? 32 : len;
    return wl_ioctl_call(WLC_SET_VAR, buf, (uint32_t)send_len);
}

static void add_match(unsigned char *buf, size_t base, uint32_t offset, uint32_t size,
                      const unsigned char *mask, const unsigned char *pattern) {
    put_le32(buf + base + 0, offset);
    put_le32(buf + base + 4, size);
    memcpy(buf + base + 8, mask, size);
    memcpy(buf + base + 8 + size, pattern, size);
}

static size_t build_tcp_filter(unsigned char *buf, size_t buf_len, uint32_t filter_id,
                               uint16_t port) {
    const unsigned char mask0_4[4] = {0, 0, 0, 0};
    const unsigned char mask0_2[2] = {0, 0};
    const unsigned char maskff_2[2] = {0xff, 0xff};
    const unsigned char zero4[4] = {0, 0, 0, 0};
    const unsigned char zero2[2] = {0, 0};
    unsigned char port_be[2];
    uint16_t tcp_port = htons(port);
    uint16_t tail_len = 0;
    uint16_t match_bytes = (uint16_t)((tail_len + 4U) * 2U);
    size_t len = 0x57U + match_bytes;

    if (len > buf_len) {
        return 0;
    }

    memset(buf, 0, buf_len);
    memcpy(buf, "pkt_filter_add", 14);
    buf[0x0e] = 0;

    put_le32(buf + 0x0f, filter_id);
    put_le32(buf + 0x13, 2);
    put_le32(buf + 0x17, 0);
    put_le16(buf + 0x1b, 5);
    put_le16(buf + 0x1d, (uint16_t)(match_bytes + 0x3cU));

    add_match(buf, 0x1f, 12, 4, mask0_4, zero4);
    add_match(buf, 0x2f, 16, 4, mask0_4, zero4);
    add_match(buf, 0x3f, 0, 2, mask0_2, zero2);
    memcpy(port_be, &tcp_port, 2);
    add_match(buf, 0x4b, 2, 2, maskff_2, port_be);

    put_le16(buf + 0x57, 13);
    put_le16(buf + 0x5b, tail_len);
    return len;
}

static int add_tcp_filter(uint32_t filter_id, uint16_t port) {
    unsigned char buf[512];
    size_t len;

    len = build_tcp_filter(buf, sizeof(buf), filter_id, port);
    if (len == 0) {
        fprintf(stderr, "failed to build pkt_filter_add buffer\n");
        return -1;
    }

    printf("pkt_filter_add iface=%s id=%u dst=any:%u len=%u\n", FILTER_IFACE, filter_id,
           port, (unsigned)len);
    return wl_ioctl_call(WLC_SET_VAR, buf, (uint32_t)len);
}

static int enable_filter(uint32_t filter_id) {
    unsigned char args[8];

    put_le32(args + 0, filter_id);
    put_le32(args + 4, 1);
    printf("pkt_filter_enable iface=%s id=%u enable=1\n", FILTER_IFACE, filter_id);
    return set_iovar("pkt_filter_enable", args, sizeof(args));
}

int main(int argc, char **argv) {
    int ret = 0;

    if (argc != 1) {
        fprintf(stderr, "usage: %s\n", argv[0]);
        return 2;
    }

    if (add_tcp_filter(TELNET_FILTER_ID, TELNET_PORT) < 0) {
        ret = 1;
    }
    if (enable_filter(TELNET_FILTER_ID) < 0) {
        ret = 1;
    }

    if (add_tcp_filter(STREAM_FILTER_ID, STREAM_RELAY_PORT) < 0) {
        ret = 1;
    }
    if (enable_filter(STREAM_FILTER_ID) < 0) {
        ret = 1;
    }

    if (add_tcp_filter(RTSP_FILTER_ID, RTSP_PORT) < 0) {
        ret = 1;
    }
    if (enable_filter(RTSP_FILTER_ID) < 0) {
        ret = 1;
    }

    if (add_tcp_filter(ONVIF_FILTER_ID, ONVIF_HTTP_PORT) < 0) {
        ret = 1;
    }
    if (enable_filter(ONVIF_FILTER_ID) < 0) {
        ret = 1;
    }

    if (add_tcp_filter(WSD_FILTER_ID, WSD_PORT) < 0) {
        ret = 1;
    }
    if (enable_filter(WSD_FILTER_ID) < 0) {
        ret = 1;
    }

    return ret;
}
