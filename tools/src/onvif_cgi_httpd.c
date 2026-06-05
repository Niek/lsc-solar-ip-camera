#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define LISTEN_PORT 8899
#define MAX_HEADER 8192
#define MAX_BODY 16384
#define MAX_CGI_OUTPUT 131072

#define ONVIF_ROOT "/tmp/mnt/sdcard/custom/onvif"
#define ONVIF_CGI "/tmp/mnt/sdcard/custom/bin/onvif_simple_server"
#define ONVIF_CONF "/tmp/mnt/sdcard/custom/onvif/onvif_simple_server.conf"

static int write_all(int fd, const void *buf, size_t len) {
    const char *p = (const char *)buf;

    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static void send_response(int fd, const char *status, const char *type, const char *body) {
    char header[256];
    size_t body_len = strlen(body);
    int n = snprintf(header, sizeof(header),
                     "HTTP/1.1 %s\r\n"
                     "Content-Type: %s\r\n"
                     "Content-Length: %zu\r\n"
                     "Connection: close\r\n\r\n",
                     status, type, body_len);

    if (n > 0) {
        write_all(fd, header, (size_t)n);
    }
    write_all(fd, body, body_len);
}

static char *find_header_end(char *buf, size_t len, size_t *header_len) {
    size_t i;

    for (i = 0; i + 3 < len; i++) {
        if (buf[i] == '\r' && buf[i + 1] == '\n' && buf[i + 2] == '\r' && buf[i + 3] == '\n') {
            *header_len = i + 4;
            return buf + i;
        }
    }
    for (i = 0; i + 1 < len; i++) {
        if (buf[i] == '\n' && buf[i + 1] == '\n') {
            *header_len = i + 2;
            return buf + i;
        }
    }
    return NULL;
}

static int header_name_eq(const char *line, const char *name) {
    size_t name_len = strlen(name);

    return strncasecmp(line, name, name_len) == 0 && line[name_len] == ':';
}

static int parse_http_status_line(const char *line, const char **status) {
    const char *value;

    if (strncasecmp(line, "HTTP/", 5) != 0) {
        return 0;
    }
    value = strchr(line, ' ');
    if (value == NULL) {
        return 0;
    }
    value++;
    while (isspace((unsigned char)*value)) {
        value++;
    }
    if (*value == '\0') {
        return 0;
    }
    *status = value;
    return 1;
}

static long parse_content_length(char *headers) {
    char *line = headers;

    while (line != NULL && *line != '\0') {
        char *next = strchr(line, '\n');
        if (next != NULL) {
            *next = '\0';
        }
        while (*line == '\r' || *line == '\n' || isspace((unsigned char)*line)) {
            line++;
        }
        if (header_name_eq(line, "Content-Length")) {
            char *value = strchr(line, ':');
            if (value == NULL) {
                return -1;
            }
            value++;
            while (isspace((unsigned char)*value)) {
                value++;
            }
            return strtol(value, NULL, 10);
        }
        if (next == NULL) {
            break;
        }
        line = next + 1;
    }
    return 0;
}

static int valid_service(const char *service) {
    static const char *const services[] = {
        "device_service",
        "media_service",
        "media2_service",
        "ptz_service",
        "events_service",
        "deviceio_service",
    };
    size_t i;

    for (i = 0; i < sizeof(services) / sizeof(services[0]); i++) {
        if (strcmp(service, services[i]) == 0) {
            return 1;
        }
    }
    return 0;
}

static int extract_service(const char *path, char *service, size_t service_len) {
    const char *prefix = "/onvif/";
    const char *start;
    const char *end;
    size_t len;

    if (strncmp(path, prefix, strlen(prefix)) != 0) {
        return -1;
    }
    start = path + strlen(prefix);
    end = start;
    while (*end != '\0' && *end != '?' && *end != '/') {
        end++;
    }
    len = (size_t)(end - start);
    if (len == 0 || len >= service_len) {
        return -1;
    }
    memcpy(service, start, len);
    service[len] = '\0';
    return valid_service(service) ? 0 : -1;
}

static void set_cgi_env(const char *method, const char *path, const char *service,
                        long content_length, const struct sockaddr_in *peer) {
    char length[32];
    char script_name[128];
    char remote_addr[64];
    char remote_port[16];

    snprintf(length, sizeof(length), "%ld", content_length);
    snprintf(script_name, sizeof(script_name), "/onvif/%s", service);
    snprintf(remote_port, sizeof(remote_port), "%u", ntohs(peer->sin_port));
    inet_ntop(AF_INET, &peer->sin_addr, remote_addr, sizeof(remote_addr));

    setenv("REQUEST_METHOD", method, 1);
    setenv("REQUEST_URI", path, 1);
    setenv("SCRIPT_NAME", script_name, 1);
    setenv("PATH_INFO", script_name, 1);
    setenv("CONTENT_TYPE", "application/soap+xml", 1);
    setenv("CONTENT_LENGTH", length, 1);
    setenv("GATEWAY_INTERFACE", "CGI/1.1", 1);
    setenv("SERVER_NAME", "t23-camera", 1);
    setenv("SERVER_PORT", "8899", 1);
    setenv("SERVER_PROTOCOL", "HTTP/1.1", 1);
    setenv("SERVER_SOFTWARE", "t23-onvif-cgi-httpd", 1);
    setenv("REMOTE_ADDR", remote_addr, 1);
    setenv("REMOTE_PORT", remote_port, 1);
    setenv("QUERY_STRING", "", 1);
}

static int read_cgi_output(int fd, char **out, size_t *out_len) {
    char *buf = (char *)malloc(MAX_CGI_OUTPUT);
    size_t total = 0;

    if (buf == NULL) {
        return -1;
    }

    while (total < MAX_CGI_OUTPUT) {
        ssize_t n = read(fd, buf + total, MAX_CGI_OUTPUT - total);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(buf);
            return -1;
        }
        if (n == 0) {
            break;
        }
        total += (size_t)n;
    }

    *out = buf;
    *out_len = total;
    return total == MAX_CGI_OUTPUT ? -1 : 0;
}

static void send_cgi_response(int client_fd, char *cgi, size_t cgi_len) {
    const char *status = "200 OK";
    size_t header_len = 0;
    char *headers;
    char *filtered;
    size_t filtered_len = 0;
    char *line;

    if (find_header_end(cgi, cgi_len, &header_len) == NULL) {
        send_response(client_fd, "502 Bad Gateway", "text/plain", "CGI returned malformed output\n");
        return;
    }

    headers = (char *)malloc(header_len + 1);
    filtered = (char *)malloc(header_len + 64);
    if (headers == NULL || filtered == NULL) {
        free(headers);
        free(filtered);
        send_response(client_fd, "500 Internal Server Error", "text/plain", "out of memory\n");
        return;
    }
    memcpy(headers, cgi, header_len);
    headers[header_len] = '\0';

    line = headers;
    while (line != NULL && *line != '\0') {
        char *next = strchr(line, '\n');
        size_t line_len;
        if (next != NULL) {
            *next = '\0';
        }
        while (*line == '\r' || *line == '\n') {
            line++;
        }
        line_len = strlen(line);
        while (line_len > 0 && (line[line_len - 1] == '\r' || line[line_len - 1] == '\n')) {
            line[--line_len] = '\0';
        }
        if (line_len > 0) {
            if (header_name_eq(line, "Status")) {
                char *value = strchr(line, ':');
                if (value != NULL) {
                    value++;
                    while (isspace((unsigned char)*value)) {
                        value++;
                    }
                    status = value;
                }
            } else if (parse_http_status_line(line, &status)) {
                /* roleoroleo emits raw HTTP status lines for some SOAP faults. */
            } else if (strchr(line, ':') == NULL) {
                /* Do not forward invalid CGI header lines as HTTP headers. */
            } else {
                memcpy(filtered + filtered_len, line, line_len);
                filtered_len += line_len;
                memcpy(filtered + filtered_len, "\r\n", 2);
                filtered_len += 2;
            }
        }
        if (next == NULL) {
            break;
        }
        line = next + 1;
    }

    dprintf(client_fd, "HTTP/1.1 %s\r\n", status);
    write_all(client_fd, filtered, filtered_len);
    write_all(client_fd, "\r\n", 2);
    write_all(client_fd, cgi + header_len, cgi_len - header_len);

    free(headers);
    free(filtered);
}

static void run_cgi(int client_fd, const char *path, const char *service, const char *body,
                    long body_len, const struct sockaddr_in *peer) {
    int in_pipe[2];
    int out_pipe[2];
    pid_t pid;
    char *cgi = NULL;
    size_t cgi_len = 0;

    if (pipe(in_pipe) < 0 || pipe(out_pipe) < 0) {
        send_response(client_fd, "500 Internal Server Error", "text/plain", "pipe failed\n");
        return;
    }

    pid = fork();
    if (pid < 0) {
        close(in_pipe[0]);
        close(in_pipe[1]);
        close(out_pipe[0]);
        close(out_pipe[1]);
        send_response(client_fd, "500 Internal Server Error", "text/plain", "fork failed\n");
        return;
    }

    if (pid == 0) {
        dup2(in_pipe[0], STDIN_FILENO);
        dup2(out_pipe[1], STDOUT_FILENO);
        close(in_pipe[0]);
        close(in_pipe[1]);
        close(out_pipe[0]);
        close(out_pipe[1]);

        set_cgi_env("POST", path, service, body_len, peer);
        if (chdir(ONVIF_ROOT) < 0) {
            _exit(111);
        }
        execl(ONVIF_CGI, "onvif_simple_server", "-c", ONVIF_CONF, service, (char *)NULL);
        _exit(127);
    }

    close(in_pipe[0]);
    close(out_pipe[1]);
    if (body_len > 0) {
        write_all(in_pipe[1], body, (size_t)body_len);
    }
    close(in_pipe[1]);

    if (read_cgi_output(out_pipe[0], &cgi, &cgi_len) == 0 && cgi_len > 0) {
        send_cgi_response(client_fd, cgi, cgi_len);
    } else {
        send_response(client_fd, "502 Bad Gateway", "text/plain", "CGI failed\n");
    }
    free(cgi);
    close(out_pipe[0]);
    waitpid(pid, NULL, 0);
}

static void handle_client(int client_fd, const struct sockaddr_in *peer) {
    char header[MAX_HEADER + 1];
    char method[16];
    char path[256];
    char version[16];
    char service[64];
    char *headers;
    char *body;
    size_t total = 0;
    size_t header_len = 0;
    long content_length;
    ssize_t n;

    memset(method, 0, sizeof(method));
    memset(path, 0, sizeof(path));
    memset(version, 0, sizeof(version));

    while (total < MAX_HEADER) {
        n = recv(client_fd, header + total, MAX_HEADER - total, 0);
        if (n <= 0) {
            return;
        }
        total += (size_t)n;
        header[total] = '\0';
        if (find_header_end(header, total, &header_len) != NULL) {
            break;
        }
    }
    if (header_len == 0) {
        send_response(client_fd, "431 Request Header Fields Too Large", "text/plain", "headers too large\n");
        return;
    }

    if (sscanf(header, "%15s %255s %15s", method, path, version) != 3) {
        send_response(client_fd, "400 Bad Request", "text/plain", "bad request\n");
        return;
    }
    (void)version;
    if (strcmp(method, "POST") != 0) {
        send_response(client_fd, "405 Method Not Allowed", "text/plain", "POST required\n");
        return;
    }
    if (extract_service(path, service, sizeof(service)) < 0) {
        send_response(client_fd, "404 Not Found", "text/plain", "unknown ONVIF service\n");
        return;
    }

    headers = (char *)malloc(header_len + 1);
    if (headers == NULL) {
        send_response(client_fd, "500 Internal Server Error", "text/plain", "out of memory\n");
        return;
    }
    memcpy(headers, header, header_len);
    headers[header_len] = '\0';
    content_length = parse_content_length(headers);
    free(headers);

    if (content_length <= 0 || content_length > MAX_BODY) {
        send_response(client_fd, "413 Payload Too Large", "text/plain", "invalid content length\n");
        return;
    }

    body = (char *)malloc((size_t)content_length + 1);
    if (body == NULL) {
        send_response(client_fd, "500 Internal Server Error", "text/plain", "out of memory\n");
        return;
    }

    {
        size_t already = total - header_len;
        if (already > (size_t)content_length) {
            already = (size_t)content_length;
        }
        memcpy(body, header + header_len, already);
        while (already < (size_t)content_length) {
            n = recv(client_fd, body + already, (size_t)content_length - already, 0);
            if (n <= 0) {
                free(body);
                return;
            }
            already += (size_t)n;
        }
    }
    body[content_length] = '\0';

    run_cgi(client_fd, path, service, body, content_length, peer);
    free(body);
}

int main(int argc, char **argv) {
    int listen_fd;
    int opt = 1;
    struct sockaddr_in addr;

    if (argc != 1) {
        fprintf(stderr, "usage: %s\n", argv[0]);
        return 2;
    }

    signal(SIGCHLD, SIG_IGN);
    listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        perror("socket");
        return 1;
    }
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(LISTEN_PORT);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind");
        return 1;
    }
    if (listen(listen_fd, 8) < 0) {
        perror("listen");
        return 1;
    }

    printf("onvif cgi httpd listening on %d\n", LISTEN_PORT);
    fflush(stdout);

    for (;;) {
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);
        int client_fd = accept(listen_fd, (struct sockaddr *)&peer, &peer_len);
        pid_t pid;

        if (client_fd < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("accept");
            continue;
        }

        pid = fork();
        if (pid == 0) {
            close(listen_fd);
            handle_client(client_fd, &peer);
            close(client_fd);
            _exit(0);
        }
        close(client_fd);
    }
}
