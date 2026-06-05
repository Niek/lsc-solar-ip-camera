#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DOCKER_IMAGE:-lsc-tuya-mipsel-builder:bookworm}"
DOCKERFILE="$ROOT/tools/docker/mipsel-builder.Dockerfile"
SMOLRTSP_DIR="$ROOT/build/third_party/smolrtsp"
SMOLRTSP_REF="${SMOLRTSP_REF:-40de2491e0b3370e276f864d7b65a057a433296a}"
ONVIF_DIR="$ROOT/build/third_party/onvif_simple_server"
ONVIF_REF="${ONVIF_REF:-45dbabf40bcd16e9063dff0a52a31f494a439ec1}"
ONVIF_PATCH="$ROOT/tools/patches/onvif_simple_server-no-json-zlib.patch"

build_args=(build -t "$IMAGE" -f "$DOCKERFILE" "$ROOT")
run_args=(run --rm -v "$ROOT:/work" -w /work)
if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  build_args=(build --platform "$DOCKER_PLATFORM" -t "$IMAGE" -f "$DOCKERFILE" "$ROOT")
  run_args+=(--platform "$DOCKER_PLATFORM")
fi

if [[ ! -d "$SMOLRTSP_DIR/.git" ]]; then
  rm -rf "$SMOLRTSP_DIR"
  git clone --depth 1 https://github.com/OpenIPC/smolrtsp.git "$SMOLRTSP_DIR"
fi
git -C "$SMOLRTSP_DIR" fetch --depth 1 origin "$SMOLRTSP_REF" >/dev/null 2>&1 || true
git -C "$SMOLRTSP_DIR" checkout --detach "$SMOLRTSP_REF" >/dev/null

if [[ ! -d "$ONVIF_DIR/.git" ]]; then
  rm -rf "$ONVIF_DIR"
  git clone --depth 1 https://github.com/roleoroleo/onvif_simple_server.git "$ONVIF_DIR"
fi
git -C "$ONVIF_DIR" fetch --depth 1 origin "$ONVIF_REF" >/dev/null 2>&1 || true
git -C "$ONVIF_DIR" checkout --detach "$ONVIF_REF" >/dev/null
git -C "$ONVIF_DIR" reset --hard >/dev/null
git -C "$ONVIF_DIR" clean -fdx >/dev/null
git -C "$ONVIF_DIR" apply "$ONVIF_PATCH"

docker "${build_args[@]}"

docker "${run_args[@]}" "$IMAGE" sh -eu -c '
  mkdir -p build/mipsel/bin
  cmake -S build/third_party/smolrtsp -B build/mipsel/smolrtsp \
    -DCMAKE_C_COMPILER=mipsel-linux-gnu-gcc \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DSMOLRTSP_WITH_H264=ON \
    -DSMOLRTSP_WITH_H265=OFF \
    -DSMOLRTSP_WITH_H266=OFF \
    -DSMOLRTSP_WITH_AV1=OFF \
    -DSMOLRTSP_WITH_JPEG=OFF \
    -DSMOLRTSP_WITH_JPEGXS=OFF
  cmake --build build/mipsel/smolrtsp --target smolrtsp -j"$(nproc)"

  smolrtsp_includes="
    -Ibuild/third_party/smolrtsp/include
    -Ibuild/mipsel/smolrtsp/_deps/slice99-src
    -Ibuild/mipsel/smolrtsp/_deps/metalang99-src/include
    -Ibuild/mipsel/smolrtsp/_deps/datatype99-src
    -Ibuild/mipsel/smolrtsp/_deps/interface99-src
  "

  mipsel-linux-gnu-gcc \
    -mips32r2 -mabi=32 -static -Os -s \
    -Wall -Wextra -Werror \
    -o build/mipsel/bin/aic_filter \
    tools/src/aic_filter.c

  mipsel-linux-gnu-gcc \
    -mips32r2 -mabi=32 -static -Os -s \
    -Wall -Wextra -Werror \
    -DSMOLRTSP_WITH_H264 \
    $smolrtsp_includes \
    -o build/mipsel/bin/stone_dump_relay \
    tools/src/stone_dump_relay.c \
    build/mipsel/smolrtsp/libsmolrtsp.a

  mipsel-linux-gnu-gcc \
    -mips32r2 -mabi=32 -static -Os -s \
    -Wall -Wextra -Werror \
    -o build/mipsel/bin/onvif_cgi_httpd \
    tools/src/onvif_cgi_httpd.c

  mipsel-linux-gnu-gcc \
    -mips32r2 -mabi=32 -static -Os -s \
    -Wall -Wextra -Werror \
    -o build/mipsel/bin/patch_stone_main \
    tools/src/patch_stone_main.c

  onvif_src=build/third_party/onvif_simple_server
  onvif_build=build/mipsel/onvif_simple_server
  rm -rf "$onvif_build"
  mkdir -p "$onvif_build"
  (
    cd "$onvif_build"
    cc=mipsel-linux-gnu-gcc
    cflags="-mips32r2 -mabi=32 -Os -fPIC -ffunction-sections -fdata-sections -DHAVE_MBEDTLS -DT23_NO_ONVIF_JSON -I../../third_party/onvif_simple_server -I../../third_party/onvif_simple_server/ezxml"
    onvif_sources="onvif_simple_server.c device_service.c media_service.c media2_service.c ptz_service.c events_service.c deviceio_service.c fault.c conf.c utils.c log.c ezxml_wrapper.c ezxml/ezxml.c"
    for src in $onvif_sources; do
      obj="$(basename "${src%.c}").o"
      "$cc" $cflags -c "../../third_party/onvif_simple_server/$src" -o "$obj"
    done
    "$cc" -static -Os -s -Wl,--gc-sections \
      -o ../bin/onvif_simple_server \
      onvif_simple_server.o device_service.o media_service.o media2_service.o \
      ptz_service.o events_service.o deviceio_service.o fault.o conf.o utils.o \
      log.o ezxml_wrapper.o ezxml.o -lmbedcrypto -lpthread -lrt

    rm -f *.o
    wsd_sources="wsd_simple_server.c utils.c log.c ezxml_wrapper.c ezxml/ezxml.c"
    for src in $wsd_sources; do
      obj="$(basename "${src%.c}").o"
      "$cc" $cflags -c "../../third_party/onvif_simple_server/$src" -o "$obj"
    done
    "$cc" -static -Os -s -Wl,--gc-sections \
      -o ../bin/wsd_simple_server \
      wsd_simple_server.o utils.o log.o ezxml_wrapper.o ezxml.o -lmbedcrypto -lpthread -lrt
  )

  file build/mipsel/bin/aic_filter
  file build/mipsel/bin/stone_dump_relay
  file build/mipsel/bin/onvif_cgi_httpd
  file build/mipsel/bin/patch_stone_main
  file build/mipsel/bin/onvif_simple_server
  file build/mipsel/bin/wsd_simple_server
'
