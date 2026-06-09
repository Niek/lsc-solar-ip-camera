#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <smolrtsp.h>
#include <smolrtsp/nal/h264.h>

#define RAW_PORT 8555
#define RTSP_PORT 8554
#define DUMP_DIR "/mnt/mountdir"
#define DUMP_PATH "/mnt/mountdir/dump.bs"
#define TRUNCATE_AFTER (512 * 1024)
#define MOTION_FILE "/tmp/onvif_notify_server/motion_alarm"
#define MOTION_LOG "/tmp/mnt/sdcard/logs/video_motion.log"
#define MOTION_LOCK_PATH "/tmp/stone_dump_relay.motion.lock"

#define RTSP_SESSION "12345678"
#define RTP_PAYLOAD_TYPE 96
#define RTP_SSRC 0x5344554dU
#define RTP_CLOCK 90000U
#define VIDEO_FPS 15U
#define RTP_TS_STEP (RTP_CLOCK / VIDEO_FPS)
#define NAL_BUFFER_SIZE (1024 * 1024)
#define MOTION_WARMUP_FRAMES 120U
#define MOTION_HOLD_SECONDS 8
#define MOTION_MIN_SAMPLE_BYTES 18000U
#define MOTION_ABS_DELTA_BYTES 8000U
#define LISTEN_BACKLOG 8
#define CLIENT_IO_TIMEOUT_SECONDS 5
#define STREAM_COUNT_LOCK_PATH "/tmp/stone_dump_relay.stream_count.lock"
#define STREAM_COUNT_PATH "/tmp/stone_dump_relay.stream_count"

typedef struct {
    int fd;
} SocketWriter;

typedef struct {
    int fd;
    SocketWriter writer;
    SmolRTSP_NalTransport *nal_transport;
} RtspSink;

typedef struct {
    unsigned int samples;
    uint32_t ema_q8;
    time_t active_until;
    int active;
} MotionDetector;

static int send_all(int fd, const void *buf, size_t len) {
    const unsigned char *p = buf;
    size_t pos = 0;

    while (pos < len) {
        ssize_t n = send(fd, p + pos, len - pos, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            return -1;
        }
        pos += (size_t)n;
    }
    return 0;
}

static void motion_log(const char *state, size_t sample, uint32_t avg) {
    FILE *fp = fopen(MOTION_LOG, "a");
    time_t now;

    if (fp == NULL) {
        return;
    }
    now = time(NULL);
    fprintf(fp, "%ld %s sample=%lu avg=%lu\n", (long)now, state,
            (unsigned long)sample, (unsigned long)avg);
    fclose(fp);
}

static void set_motion_file(int active, size_t sample, uint32_t avg) {
    if (active) {
        int fd;
        mkdir("/tmp/onvif_notify_server", 0777);
        fd = open(MOTION_FILE, O_CREAT | O_WRONLY, 0666);
        if (fd >= 0) {
            close(fd);
        }
        motion_log("motion-on", sample, avg);
    } else {
        unlink(MOTION_FILE);
        motion_log("motion-off", sample, avg);
    }
}

static int byte_motion_enabled(void) {
    const char *value = getenv("STONE_MOTION_BYTES");

    return value == NULL || strcmp(value, "0") != 0;
}

static void motion_detector_update(MotionDetector *det, size_t sample) {
    uint32_t avg;
    size_t threshold;
    time_t now;

    if (sample == 0) {
        return;
    }

    now = time(NULL);
    if (det->active && now >= det->active_until) {
        det->active = 0;
        set_motion_file(0, sample, det->ema_q8 >> 8);
    }

    if (det->ema_q8 == 0) {
        det->ema_q8 = (uint32_t)sample << 8;
    }
    avg = det->ema_q8 >> 8;
    threshold = (size_t)avg + (avg / 3U) + MOTION_ABS_DELTA_BYTES;

    if (det->samples >= MOTION_WARMUP_FRAMES &&
        sample > MOTION_MIN_SAMPLE_BYTES && sample > threshold) {
        det->active_until = now + MOTION_HOLD_SECONDS;
        if (!det->active) {
            det->active = 1;
            set_motion_file(1, sample, avg);
        }
    }

    if (det->samples < MOTION_WARMUP_FRAMES || !det->active) {
        det->ema_q8 = ((det->ema_q8 * 15U) + ((uint32_t)sample << 8)) / 16U;
    }
    det->samples++;
}

static ssize_t SocketWriter_write(VSelf, CharSlice99 data) {
    VSELF(SocketWriter);

    if (send_all(self->fd, data.ptr, data.len) < 0) {
        return -1;
    }
    return (ssize_t)data.len;
}

static void SocketWriter_lock(VSelf) {
    VSELF(SocketWriter);
    (void)self;
}

static void SocketWriter_unlock(VSelf) {
    VSELF(SocketWriter);
    (void)self;
}

static size_t SocketWriter_filled(VSelf) {
    VSELF(SocketWriter);
    (void)self;
    return 0;
}

static int SocketWriter_vwritef(VSelf, const char *restrict fmt, va_list ap) {
    VSELF(SocketWriter);
    char stack_buf[1024];
    char *buf = stack_buf;
    va_list copy;
    int needed;
    int ret;

    va_copy(copy, ap);
    needed = vsnprintf(stack_buf, sizeof(stack_buf), fmt, copy);
    va_end(copy);
    if (needed < 0) {
        return -1;
    }
    if ((size_t)needed >= sizeof(stack_buf)) {
        buf = malloc((size_t)needed + 1);
        if (buf == NULL) {
            return -1;
        }
        ret = vsnprintf(buf, (size_t)needed + 1, fmt, ap);
        if (ret != needed) {
            free(buf);
            return -1;
        }
    }

    ret = send_all(self->fd, buf, (size_t)needed) < 0 ? -1 : needed;
    if (buf != stack_buf) {
        free(buf);
    }
    return ret;
}

static int SocketWriter_writef(VSelf, const char *restrict fmt, ...) {
    VSELF(SocketWriter);
    va_list ap;
    int ret;

    va_start(ap, fmt);
    ret = SocketWriter_vwritef(self, fmt, ap);
    va_end(ap);
    return ret;
}

impl(SmolRTSP_Writer, SocketWriter);

static int make_server(int port) {
    int fd;
    int yes = 1;
    struct sockaddr_in addr;

    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        close(fd);
        return -1;
    }
    if (listen(fd, LISTEN_BACKLOG) < 0) {
        perror("listen");
        close(fd);
        return -1;
    }
    return fd;
}

static void set_client_timeouts(int fd) {
    struct timeval tv;

    tv.tv_sec = CLIENT_IO_TIMEOUT_SECONDS;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

static int lock_count_fd(int fd) {
    struct flock fl;
    memset(&fl, 0, sizeof(fl));
    fl.l_type = F_WRLCK;
    fl.l_whence = SEEK_SET;
    return fcntl(fd, F_SETLKW, &fl);
}

static int try_lock_motion_fd(void) {
    struct flock fl;
    int fd = open(MOTION_LOCK_PATH, O_CREAT | O_RDWR, 0666);

    if (fd < 0) {
        return -1;
    }
    memset(&fl, 0, sizeof(fl));
    fl.l_type = F_WRLCK;
    fl.l_whence = SEEK_SET;
    if (fcntl(fd, F_SETLK, &fl) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int update_stream_count(int delta) {
    char buf[32];
    int lock_fd;
    int count_fd;
    int count = 0;
    ssize_t n;

    lock_fd = open(STREAM_COUNT_LOCK_PATH, O_CREAT | O_RDWR, 0666);
    if (lock_fd < 0) {
        perror("open stream count lock");
        return 1;
    }
    if (lock_count_fd(lock_fd) < 0) {
        perror("stream count lock");
        close(lock_fd);
        return 1;
    }

    count_fd = open(STREAM_COUNT_PATH, O_CREAT | O_RDWR, 0666);
    if (count_fd >= 0) {
        n = read(count_fd, buf, sizeof(buf) - 1);
        if (n > 0) {
            buf[n] = '\0';
            count = atoi(buf);
        }
    }

    count += delta;
    if (count < 0) {
        count = 0;
    }
    if (count_fd >= 0) {
        snprintf(buf, sizeof(buf), "%d\n", count);
        lseek(count_fd, 0, SEEK_SET);
        ftruncate(count_fd, 0);
        if (write(count_fd, buf, strlen(buf)) < 0) {
            perror("write stream count");
        }
        close(count_fd);
    }

    close(lock_fd);
    return count;
}

static int client_closed(int fd) {
    fd_set rfds;
    struct timeval tv;
    char c;
    int ret;

    FD_ZERO(&rfds);
    FD_SET(fd, &rfds);
    tv.tv_sec = 0;
    tv.tv_usec = 0;

    ret = select(fd + 1, &rfds, NULL, NULL, &tv);
    if (ret <= 0 || !FD_ISSET(fd, &rfds)) {
        return 0;
    }

    ret = (int)recv(fd, &c, 1, MSG_PEEK);
    return ret == 0 || (ret < 0 && errno != EAGAIN && errno != EINTR);
}

static int create_trigger(void) {
    int fd;

    mkdir(DUMP_DIR, 0777);
    fd = open(DUMP_PATH, O_CREAT | O_RDWR, 0666);
    if (fd < 0) {
        perror("open dump trigger");
        return -1;
    }
    close(fd);
    return 0;
}

static int reopen_dump(int old_fd) {
    int fd;

    if (old_fd >= 0) {
        close(old_fd);
    }
    fd = open(DUMP_PATH, O_RDONLY);
    if (fd < 0) {
        perror("open dump read");
    }
    return fd;
}

static int truncate_dump(int old_fd) {
    int fd;

    if (old_fd >= 0) {
        close(old_fd);
    }
    fd = open(DUMP_PATH, O_WRONLY | O_TRUNC);
    if (fd >= 0) {
        close(fd);
    }
    return reopen_dump(-1);
}

static void relay_raw_client(int cfd) {
    unsigned char buf[8192];
    int dfd = -1;
    off_t offset = 0;

    printf("raw client connected\n");
    if (create_trigger() < 0) {
        return;
    }
    update_stream_count(1);
    dfd = reopen_dump(-1);
    if (dfd < 0) {
        if (update_stream_count(-1) == 0) {
            unlink(DUMP_PATH);
        }
        return;
    }

    while (!client_closed(cfd)) {
        struct stat st;

        if (stat(DUMP_PATH, &st) < 0) {
            usleep(20000);
            continue;
        }
        if (st.st_size < offset) {
            offset = 0;
            dfd = reopen_dump(dfd);
            if (dfd < 0) {
                break;
            }
        }
        if (st.st_size > offset) {
            ssize_t want = st.st_size - offset;
            ssize_t n;
            if (want > (ssize_t)sizeof(buf)) {
                want = (ssize_t)sizeof(buf);
            }
            n = pread(dfd, buf, (size_t)want, offset);
            if (n < 0) {
                if (errno == EINTR) {
                    continue;
                }
                perror("pread");
                break;
            }
            if (n > 0) {
                if (send_all(cfd, buf, (size_t)n) < 0) {
                    break;
                }
                offset += n;
            }
            if (offset >= TRUNCATE_AFTER && offset >= st.st_size) {
                dfd = truncate_dump(dfd);
                if (dfd < 0) {
                    break;
                }
                offset = 0;
            }
            continue;
        }
        usleep(20000);
    }

    if (dfd >= 0) {
        close(dfd);
    }
    if (update_stream_count(-1) == 0) {
        unlink(DUMP_PATH);
    }
    printf("raw client disconnected\n");
}

static int method_is(const SmolRTSP_Request *req, const char *method) {
    size_t len = strlen(method);

    return req->start_line.method.len == len &&
           memcmp(req->start_line.method.ptr, method, len) == 0;
}

static int read_rtsp_request(int fd, char *buf, size_t buf_len,
                             SmolRTSP_Request *req) {
    size_t pos = 0;

    while (pos + 1 < buf_len) {
        SmolRTSP_ParseResult result;
        ssize_t n = recv(fd, buf + pos, buf_len - pos - 1, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            return 0;
        }
        pos += (size_t)n;
        buf[pos] = '\0';

        *req = SmolRTSP_Request_uninit();
        result = SmolRTSP_Request_parse(req, CharSlice99_new(buf, pos));
        if (SmolRTSP_ParseResult_is_complete(result)) {
            return 1;
        }
        if (SmolRTSP_ParseResult_is_failure(result)) {
            return -1;
        }
    }
    return -1;
}

static ssize_t rtsp_respond(RtspSink *sink, const SmolRTSP_Request *req,
                            SmolRTSP_StatusCode code, const char *reason) {
    SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
    SmolRTSP_Context *ctx = SmolRTSP_Context_new(writer, req->cseq);
    ssize_t ret;

    smolrtsp_header(ctx, SMOLRTSP_HEADER_SERVER, "LSC-Tuya-smolrtsp");
    ret = smolrtsp_respond(ctx, code, reason);
    VTABLE(SmolRTSP_Context, SmolRTSP_Droppable).drop(ctx);
    return ret;
}

static ssize_t rtsp_options(RtspSink *sink, const SmolRTSP_Request *req) {
    SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
    SmolRTSP_Context *ctx = SmolRTSP_Context_new(writer, req->cseq);
    ssize_t ret;

    smolrtsp_header(ctx, SMOLRTSP_HEADER_SERVER, "LSC-Tuya-smolrtsp");
    smolrtsp_header(ctx, SMOLRTSP_HEADER_PUBLIC,
                    "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER");
    ret = smolrtsp_respond_ok(ctx);
    VTABLE(SmolRTSP_Context, SmolRTSP_Droppable).drop(ctx);
    return ret;
}

static ssize_t rtsp_describe(RtspSink *sink, const SmolRTSP_Request *req) {
    static char sdp[] =
        "v=0\r\n"
        "o=- 0 0 IN IP4 0.0.0.0\r\n"
        "s=LSC Tuya Camera\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        "a=control:*\r\n"
        "m=video 0 RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=fmtp:96 packetization-mode=1;profile-level-id=640028\r\n"
        "a=framerate:15\r\n"
        "a=control:trackID=0\r\n";
    SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
    SmolRTSP_Context *ctx = SmolRTSP_Context_new(writer, req->cseq);
    ssize_t ret;

    smolrtsp_header(ctx, SMOLRTSP_HEADER_SERVER, "LSC-Tuya-smolrtsp");
    smolrtsp_header(ctx, SMOLRTSP_HEADER_CONTENT_TYPE, "application/sdp");
    smolrtsp_body(ctx, CharSlice99_from_str(sdp));
    ret = smolrtsp_respond_ok(ctx);
    VTABLE(SmolRTSP_Context, SmolRTSP_Droppable).drop(ctx);
    return ret;
}

static void rtsp_sink_close(RtspSink *sink) {
    if (sink->nal_transport != NULL) {
        VTABLE(SmolRTSP_NalTransport, SmolRTSP_Droppable).drop(sink->nal_transport);
        sink->nal_transport = NULL;
    }
}

static int setup_udp_transport(RtspSink *sink, const struct sockaddr_in *peer,
                               const SmolRTSP_PortPair *client_port,
                               uint16_t *server_port) {
    int fd;
    struct sockaddr_in local;
    struct sockaddr_in dest;
    socklen_t local_len = sizeof(local);
    SmolRTSP_Transport transport;
    SmolRTSP_RtpTransport *rtp_transport;

    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("udp socket");
        return -1;
    }

    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    local.sin_port = 0;
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
        perror("udp bind");
        close(fd);
        return -1;
    }
    if (getsockname(fd, (struct sockaddr *)&local, &local_len) < 0) {
        perror("getsockname");
        close(fd);
        return -1;
    }

    dest = *peer;
    dest.sin_port = htons(client_port->rtp_port);
    *server_port = ntohs(local.sin_port);

    transport = smolrtsp_transport_udp_address(fd, &dest, sizeof(dest));
    rtp_transport = SmolRTSP_RtpTransport_new_with_ssrc(
        transport, RTP_PAYLOAD_TYPE, RTP_CLOCK, RTP_SSRC);
    sink->nal_transport = SmolRTSP_NalTransport_new(rtp_transport);
    return 0;
}

static int rtsp_setup(RtspSink *sink, const struct sockaddr_in *peer,
                      const SmolRTSP_Request *req) {
    CharSlice99 transport_value;
    SmolRTSP_TransportConfig config;
    bool transport_found;
    char transport_reply[160];
    uint8_t channel = 0;
    uint16_t server_port = 0;

    transport_found =
        SmolRTSP_HeaderMap_find(&req->header_map, SMOLRTSP_HEADER_TRANSPORT,
                                &transport_value);
    if (!transport_found || smolrtsp_parse_transport(&config, transport_value) < 0) {
        rtsp_respond(sink, req, SMOLRTSP_STATUS_UNSUPPORTED_TRANSPORT,
                     "Unsupported Transport");
        return -1;
    }

    rtsp_sink_close(sink);
    if (config.lower == SmolRTSP_LowerTransport_TCP) {
        SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
        SmolRTSP_Transport transport;
        SmolRTSP_RtpTransport *rtp_transport;

        ifLet(config.interleaved, SmolRTSP_ChannelPair_Some, interleaved) {
            channel = interleaved->rtp_channel;
        }
        transport = smolrtsp_transport_tcp(writer, channel, 0);
        rtp_transport = SmolRTSP_RtpTransport_new_with_ssrc(
            transport, RTP_PAYLOAD_TYPE, RTP_CLOCK, RTP_SSRC);
        sink->nal_transport = SmolRTSP_NalTransport_new(rtp_transport);
        snprintf(transport_reply, sizeof(transport_reply),
                 "RTP/AVP/TCP;unicast;interleaved=%u-%u", channel, channel + 1);
    } else {
        bool ok = false;

        ifLet(config.client_port, SmolRTSP_PortPair_Some, client_port) {
            if (setup_udp_transport(sink, peer, client_port, &server_port) == 0) {
                snprintf(transport_reply, sizeof(transport_reply),
                         "RTP/AVP;unicast;client_port=%u-%u;server_port=%u-%u",
                         client_port->rtp_port, client_port->rtcp_port, server_port,
                         server_port + 1);
                ok = true;
            }
        }
        if (!ok) {
            rtsp_respond(sink, req, SMOLRTSP_STATUS_UNSUPPORTED_TRANSPORT,
                         "Unsupported Transport");
            return -1;
        }
    }

    {
        SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
        SmolRTSP_Context *ctx = SmolRTSP_Context_new(writer, req->cseq);
        ssize_t ret;

        smolrtsp_header(ctx, SMOLRTSP_HEADER_SERVER, "LSC-Tuya-smolrtsp");
        smolrtsp_header(ctx, SMOLRTSP_HEADER_TRANSPORT, "%s", transport_reply);
        smolrtsp_header(ctx, SMOLRTSP_HEADER_SESSION, RTSP_SESSION);
        ret = smolrtsp_respond_ok(ctx);
        VTABLE(SmolRTSP_Context, SmolRTSP_Droppable).drop(ctx);
        return ret < 0 ? -1 : 0;
    }
}

static ssize_t rtsp_play(RtspSink *sink, const SmolRTSP_Request *req) {
    SmolRTSP_Writer writer = DYN(SocketWriter, SmolRTSP_Writer, &sink->writer);
    SmolRTSP_Context *ctx = SmolRTSP_Context_new(writer, req->cseq);
    ssize_t ret;

    smolrtsp_header(ctx, SMOLRTSP_HEADER_SERVER, "LSC-Tuya-smolrtsp");
    smolrtsp_header(ctx, SMOLRTSP_HEADER_SESSION, RTSP_SESSION);
    smolrtsp_header(ctx, SMOLRTSP_HEADER_RANGE, "npt=now-");
    ret = smolrtsp_respond_ok(ctx);
    VTABLE(SmolRTSP_Context, SmolRTSP_Droppable).drop(ctx);
    return ret;
}

static int send_h264_nal(RtspSink *sink, uint32_t timestamp,
                         const unsigned char *nal, size_t nal_len) {
    uint8_t nal_type;
    bool is_au_end;
    SmolRTSP_NalUnit nalu;

    if (nal_len == 0) {
        return 0;
    }
    if (sink->nal_transport == NULL) {
        return -1;
    }

    nal_type = nal[0] & 0x1f;
    is_au_end = nal_type == SMOLRTSP_H264_NAL_UNIT_CODED_SLICE_NON_IDR ||
                nal_type == SMOLRTSP_H264_NAL_UNIT_CODED_SLICE_IDR;
    nalu = (SmolRTSP_NalUnit){
        .header = SmolRTSP_NalHeader_H264(SmolRTSP_H264NalHeader_parse(nal[0])),
        .payload = U8Slice99_new((uint8_t *)nal + 1, nal_len - 1),
    };
    return SmolRTSP_NalTransport_send_packet(
        sink->nal_transport, SmolRTSP_RtpTimestamp_Raw(timestamp), is_au_end, nalu);
}

static ssize_t find_start_code(const unsigned char *buf, size_t len, size_t from,
                               size_t *start_len) {
    size_t i;

    for (i = from; i + 3 <= len; i++) {
        if (buf[i] == 0 && buf[i + 1] == 0 && buf[i + 2] == 1) {
            *start_len = 3;
            return (ssize_t)i;
        }
        if (i + 4 <= len && buf[i] == 0 && buf[i + 1] == 0 && buf[i + 2] == 0 &&
            buf[i + 3] == 1) {
            *start_len = 4;
            return (ssize_t)i;
        }
    }
    return -1;
}

static int process_nal_buffer(RtspSink *sink, MotionDetector *motion,
                              unsigned char *buf, size_t *len, uint32_t *timestamp) {
    for (;;) {
        size_t first_len = 0;
        size_t next_len = 0;
        ssize_t first = find_start_code(buf, *len, 0, &first_len);
        ssize_t next;
        size_t nal_start;
        size_t nal_len;
        uint8_t nal_type;

        if (first < 0) {
            *len = 0;
            return 0;
        }
        if (first > 0) {
            memmove(buf, buf + first, *len - (size_t)first);
            *len -= (size_t)first;
            first = 0;
        }

        next = find_start_code(buf, *len, first_len, &next_len);
        (void)next_len;
        if (next < 0) {
            return 0;
        }

        nal_start = first_len;
        nal_len = (size_t)next - nal_start;
        while (nal_len > 0 && buf[nal_start + nal_len - 1] == 0) {
            nal_len--;
        }

        if (nal_len > 0) {
            nal_type = buf[nal_start] & 0x1f;
            if (motion != NULL &&
                nal_type == SMOLRTSP_H264_NAL_UNIT_CODED_SLICE_NON_IDR) {
                motion_detector_update(motion, nal_len);
            }
            if (send_h264_nal(sink, *timestamp, buf + nal_start, nal_len) < 0) {
                return -1;
            }
            if (nal_type == SMOLRTSP_H264_NAL_UNIT_CODED_SLICE_NON_IDR ||
                nal_type == SMOLRTSP_H264_NAL_UNIT_CODED_SLICE_IDR) {
                *timestamp += RTP_TS_STEP;
            }
        }

        memmove(buf, buf + next, *len - (size_t)next);
        *len -= (size_t)next;
    }
}

static int discard_interleaved_frame(int fd) {
    unsigned char header[4];
    unsigned char scratch[256];
    uint16_t payload_len;
    uint8_t channel_id;

    if (recv(fd, header, sizeof(header), 0) != (ssize_t)sizeof(header)) {
        return -1;
    }
    smolrtsp_parse_interleaved_header(header, &channel_id, &payload_len);
    (void)channel_id;

    while (payload_len > 0) {
        ssize_t want = payload_len > sizeof(scratch) ? sizeof(scratch) : payload_len;
        ssize_t n = recv(fd, scratch, (size_t)want, 0);
        if (n <= 0) {
            return -1;
        }
        payload_len = (uint16_t)(payload_len - (uint16_t)n);
    }
    return 0;
}

static int rtsp_stream_control(RtspSink *sink) {
    fd_set rfds;
    struct timeval tv;
    unsigned char peek[4];
    ssize_t n;
    char req_buf[1024];
    SmolRTSP_Request req;

    FD_ZERO(&rfds);
    FD_SET(sink->fd, &rfds);
    tv.tv_sec = 0;
    tv.tv_usec = 0;

    if (select(sink->fd + 1, &rfds, NULL, NULL, &tv) <= 0 ||
        !FD_ISSET(sink->fd, &rfds)) {
        return 0;
    }

    n = recv(sink->fd, peek, sizeof(peek), MSG_PEEK);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
        return 1;
    }
    if (n < 0) {
        return 0;
    }
    if (peek[0] == '$') {
        return discard_interleaved_frame(sink->fd) < 0;
    }

    if (read_rtsp_request(sink->fd, req_buf, sizeof(req_buf), &req) <= 0) {
        return 1;
    }
    if (method_is(&req, "GET_PARAMETER") || method_is(&req, "OPTIONS")) {
        rtsp_respond(sink, &req, SMOLRTSP_STATUS_OK, "OK");
        return 0;
    }
    if (method_is(&req, "TEARDOWN")) {
        rtsp_respond(sink, &req, SMOLRTSP_STATUS_OK, "OK");
    }
    return 1;
}

static int stream_rtsp_h264(RtspSink *sink) {
    unsigned char read_buf[8192];
    unsigned char nal_buf[NAL_BUFFER_SIZE];
    MotionDetector motion;
    size_t nal_len = 0;
    int dfd = -1;
    int motion_lock_fd = -1;
    int detect_motion = byte_motion_enabled();
    off_t offset = 0;
    uint32_t timestamp = (uint32_t)time(NULL) * RTP_CLOCK;
    time_t next_motion_lock_try = 0;

    memset(&motion, 0, sizeof(motion));
    if (create_trigger() < 0) {
        return -1;
    }
    update_stream_count(1);
    dfd = reopen_dump(-1);
    if (dfd < 0) {
        if (update_stream_count(-1) == 0) {
            unlink(DUMP_PATH);
        }
        return -1;
    }

    while (!rtsp_stream_control(sink)) {
        struct stat st;

        if (stat(DUMP_PATH, &st) < 0) {
            usleep(20000);
            continue;
        }
        if (st.st_size < offset) {
            offset = 0;
            dfd = reopen_dump(dfd);
            if (dfd < 0) {
                break;
            }
        }
        if (st.st_size > offset) {
            ssize_t want = st.st_size - offset;
            ssize_t n;

            if (want > (ssize_t)sizeof(read_buf)) {
                want = (ssize_t)sizeof(read_buf);
            }
            n = pread(dfd, read_buf, (size_t)want, offset);
            if (n < 0) {
                if (errno == EINTR) {
                    continue;
                }
                perror("pread");
                break;
            }
            if (n > 0) {
                if (nal_len + (size_t)n > sizeof(nal_buf)) {
                    fprintf(stderr, "NAL parser buffer overflow; resyncing\n");
                    nal_len = 0;
                }
                memcpy(nal_buf + nal_len, read_buf, (size_t)n);
                nal_len += (size_t)n;
                offset += n;
                if (detect_motion && motion_lock_fd < 0) {
                    time_t now = time(NULL);
                    if (now >= next_motion_lock_try) {
                        motion_lock_fd = try_lock_motion_fd();
                        next_motion_lock_try = now + 1;
                    }
                }
                if (process_nal_buffer(sink, detect_motion && motion_lock_fd >= 0 ? &motion : NULL,
                                       nal_buf, &nal_len, &timestamp) < 0) {
                    break;
                }
            }
            if (offset >= TRUNCATE_AFTER && offset >= st.st_size && nal_len < 4096) {
                dfd = truncate_dump(dfd);
                if (dfd < 0) {
                    break;
                }
                offset = 0;
            }
            continue;
        }
        usleep(20000);
    }

    if (dfd >= 0) {
        close(dfd);
    }
    if (detect_motion && motion_lock_fd >= 0 && motion.active) {
        set_motion_file(0, 0, motion.ema_q8 >> 8);
    }
    if (motion_lock_fd >= 0) {
        close(motion_lock_fd);
    }
    if (update_stream_count(-1) == 0) {
        unlink(DUMP_PATH);
    }
    return 0;
}

static void handle_rtsp_client(int cfd, const struct sockaddr_in *peer) {
    char req_buf[4096];
    SmolRTSP_Request req;
    RtspSink sink;

    memset(&sink, 0, sizeof(sink));
    sink.fd = cfd;
    sink.writer.fd = cfd;

    printf("smolrtsp client connected\n");
    for (;;) {
        int ret = read_rtsp_request(cfd, req_buf, sizeof(req_buf), &req);
        if (ret <= 0) {
            break;
        }

        if (method_is(&req, "OPTIONS")) {
            if (rtsp_options(&sink, &req) < 0) {
                break;
            }
        } else if (method_is(&req, "DESCRIBE")) {
            if (rtsp_describe(&sink, &req) < 0) {
                break;
            }
        } else if (method_is(&req, "SETUP")) {
            if (rtsp_setup(&sink, peer, &req) < 0) {
                break;
            }
        } else if (method_is(&req, "PLAY")) {
            if (rtsp_play(&sink, &req) < 0) {
                break;
            }
            stream_rtsp_h264(&sink);
            break;
        } else if (method_is(&req, "GET_PARAMETER")) {
            if (rtsp_respond(&sink, &req, SMOLRTSP_STATUS_OK, "OK") < 0) {
                break;
            }
        } else if (method_is(&req, "TEARDOWN")) {
            rtsp_respond(&sink, &req, SMOLRTSP_STATUS_OK, "OK");
            break;
        } else {
            rtsp_respond(&sink, &req, SMOLRTSP_STATUS_METHOD_NOT_ALLOWED,
                         "Method Not Allowed");
        }
    }

    rtsp_sink_close(&sink);
    printf("smolrtsp client disconnected\n");
}

static void accept_raw(int sfd) {
    struct sockaddr_in peer;
    socklen_t peer_len = sizeof(peer);
    int cfd = accept(sfd, (struct sockaddr *)&peer, &peer_len);

    if (cfd < 0) {
        if (errno != EINTR) {
            perror("accept");
        }
        return;
    }
    set_client_timeouts(cfd);
    relay_raw_client(cfd);
    close(cfd);
}

static void accept_rtsp(int sfd) {
    struct sockaddr_in peer;
    socklen_t peer_len = sizeof(peer);
    int cfd = accept(sfd, (struct sockaddr *)&peer, &peer_len);
    pid_t pid;

    if (cfd < 0) {
        if (errno != EINTR) {
            perror("accept");
        }
        return;
    }
    set_client_timeouts(cfd);
    pid = fork();
    if (pid < 0) {
        perror("fork");
        close(cfd);
        return;
    }
    if (pid == 0) {
        close(sfd);
        handle_rtsp_client(cfd, &peer);
        close(cfd);
        _exit(0);
    }
    close(cfd);
}

int main(void) {
    int raw_sfd;
    int rtsp_sfd;

    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_IGN);
    srand((unsigned)time(NULL));
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);
    unlink(STREAM_COUNT_PATH);
    unlink(DUMP_PATH);

    raw_sfd = make_server(RAW_PORT);
    rtsp_sfd = make_server(RTSP_PORT);
    if (raw_sfd < 0 || rtsp_sfd < 0) {
        return 1;
    }

    printf("stone smolrtsp relay listening raw=%d rtsp=%d source=%s\n", RAW_PORT,
           RTSP_PORT, DUMP_PATH);
    for (;;) {
        fd_set rfds;
        int max_fd = raw_sfd > rtsp_sfd ? raw_sfd : rtsp_sfd;
        int ret;

        FD_ZERO(&rfds);
        FD_SET(raw_sfd, &rfds);
        FD_SET(rtsp_sfd, &rfds);

        ret = select(max_fd + 1, &rfds, NULL, NULL, NULL);
        if (ret < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("select");
            sleep(1);
            continue;
        }
        if (FD_ISSET(rtsp_sfd, &rfds)) {
            accept_rtsp(rtsp_sfd);
        }
        if (FD_ISSET(raw_sfd, &rfds)) {
            accept_raw(raw_sfd);
        }
    }
}
