FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN dpkg --add-architecture mipsel \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        cmake \
        file \
        gcc-mipsel-linux-gnu \
        libc6-dev-mipsel-cross \
        libmbedtls-dev:mipsel \
        make \
    && rm -rf /var/lib/apt/lists/*
