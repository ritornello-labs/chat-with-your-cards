FROM debian:trixie-slim

ARG ANKI_LAUNCHER_VERSION=25.09
ARG ANKI_LAUNCHER_URL=https://github.com/ankitects/anki/releases/download/${ANKI_LAUNCHER_VERSION}/anki-launcher-${ANKI_LAUNCHER_VERSION}-linux.tar.zst

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        libasound2 \
        libdbus-1-3 \
        libegl1 \
        libevent-2.1-7t64 \
        libfontconfig1 \
        libfreetype6 \
        libglib2.0-0 \
        libgl1 \
        liblcms2-2 \
        libminizip1t64 \
        libnss3 \
        libopus0 \
        libsnappy1v5 \
        libtiff6 \
        libwebp7 \
        libwebpdemux2 \
        libwebpmux3 \
        libxcomposite1 \
        libxcb-cursor0 \
        libxdamage1 \
        libxfixes3 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libxi6 \
        libxkbcommon-x11-0 \
        libxrandr2 \
        libxshmfence1 \
        libxtst6 \
        libxslt1.1 \
        nodejs \
        npm \
        python3 \
        python3-pip \
        python3-xlib \
        scrot \
        tar \
        xauth \
        xdg-utils \
        xdotool \
        xvfb \
        zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "${ANKI_LAUNCHER_URL}" -o /tmp/anki-launcher.tar.zst \
    && rm -rf /usr/local/share/anki \
    && mkdir -p /usr/local/share/anki /usr/local/bin \
    && tar --zstd -xf /tmp/anki-launcher.tar.zst -C /usr/local/share/anki --strip-components=1 \
    && ln -sf /usr/local/share/anki/anki /usr/local/bin/anki \
    && rm -f /tmp/anki-launcher.tar.zst

ARG ANKI_VERSION=25.09

RUN if ! printf '2\n%s\n' "${ANKI_VERSION}" | /usr/local/bin/anki >/tmp/anki-launcher-install.log 2>/tmp/anki-launcher-install.err; then \
        cat /tmp/anki-launcher-install.log /tmp/anki-launcher-install.err; \
        exit 1; \
    fi \
    && test -x /root/.local/share/AnkiProgramFiles/.venv/bin/anki \
    && rm -rf /root/.cache/uv

ARG ANKI_ADDON_WORKBENCH_SPEC="anki-addon-workbench[gui]==0.4.2"

# The workbench runs in the system interpreter. Anki itself still runs from the
# launcher-managed venv below; keep those worlds separate so GUI primitives do
# not depend on Anki's bundled Python.
RUN npm install -g sfw \
    && sfw pip3 install --break-system-packages --no-cache-dir "${ANKI_ADDON_WORKBENCH_SPEC}"

ENV QT_QPA_PLATFORM=xcb
ENV QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox
ENV QTWEBENGINE_DISABLE_SANDBOX=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
# Environment-specific Anki path (launcher-managed venv) lives here, not in code.
ENV ANKI_BIN=/root/.local/share/AnkiProgramFiles/.venv/bin/anki

WORKDIR /work

CMD ["sh", "-lc", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & xvfb_pid=$!; export DISPLAY=:99; python3 -m anki_addon_workbench smoke; status=$?; kill \"$xvfb_pid\" 2>/dev/null || true; exit \"$status\""]
