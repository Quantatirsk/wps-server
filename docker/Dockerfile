FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QT_QPA_PLATFORM=xcb \
    DISPLAY=:99 \
    LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    WPS_BATCH_MAX_FILES=10

ARG WPS_DEB_URL_BASE="https://wps-linux-personal.wpscdn.cn/wps/download/ep/Linux2023/24722/wps-office_12.1.2.24722.AK.preread.sw_612408_amd64.deb"
ARG WPS_DOWNLOAD_KEY="7f8faaaa468174dc1c9cd62e5f218a5b"
ARG FONTS_ZIP_URL="https://software.cdn.vect.one/Fonts.zip"

WORKDIR /app

RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.ustc.edu.cn@g' /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y \
        curl \
        wget \
        unzip \
        bsdextrautils \
        xdg-utils \
        x11-utils \
        dbus-x11 \
        python3 \
        python3-pip \
        python3-venv \
        locales \
        xserver-xorg-core \
        xserver-xorg-video-dummy \
        xserver-xorg-input-all \
        libxslt1.1 \
        libx11-6 \
        libxext6 \
        libxtst6 \
        libxi6 \
        libqt5core5a \
        libqt5gui5 \
        libqt5widgets5 \
        libqt5x11extras5 \
        libqt5network5 \
        libglib2.0-0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libxcb-xkb1 \
        libxkbcommon-x11-0 \
        libgl1-mesa-glx \
        libglu1-mesa \
        libxrender1 \
        libxcursor1 \
        libxcomposite1 \
        libasound2 \
        libfontconfig1 \
        libnss3 \
        libgbm1 \
        fonts-opensymbol \
        fonts-noto-cjk && \
    locale-gen zh_CN.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/lib/x86_64-linux-gnu/libtiff.so.6 /usr/lib/x86_64-linux-gnu/libtiff.so.5 && \
    ldconfig

RUN WPS_URI="$(python3 -c 'from urllib.parse import urlparse; import os; print(urlparse(os.environ["WPS_DEB_URL_BASE"]).path)')" && \
    WPS_T="$(date +%s)" && \
    WPS_K="$(printf '%s' "${WPS_DOWNLOAD_KEY}${WPS_URI}${WPS_T}" | md5sum | awk '{print $1}')" && \
    wget -O /tmp/wps.deb "${WPS_DEB_URL_BASE}?t=${WPS_T}&k=${WPS_K}" && \
    apt-get update && \
    apt-get install -y --no-install-recommends /tmp/wps.deb && \
    rm -f /tmp/wps.deb && \
    rm -rf /var/lib/apt/lists/*

RUN wget -O /tmp/Fonts.zip "${FONTS_ZIP_URL}" && \
    mkdir -p /usr/local/share/fonts/zhFonts && \
    unzip -oq /tmp/Fonts.zip -d /usr/local/share/fonts/zhFonts && \
    rm -f /tmp/Fonts.zip && \
    fc-cache -f /usr/local/share/fonts/zhFonts && \
    fc-cache -f

COPY requirements.txt /app/requirements.txt
RUN pip3 install -r /app/requirements.txt -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

RUN mkdir -p /root/.config/Kingsoft
COPY docker/conf/Office.conf /root/.config/Kingsoft/Office.conf

COPY docker/conf/xorg.conf /etc/X11/xorg-dummy.conf
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/dispatcher-entrypoint.sh /dispatcher-entrypoint.sh
RUN chmod +x /entrypoint.sh /dispatcher-entrypoint.sh

COPY app /app/app

RUN mkdir -p /workspace/jobs /workspace/runtime

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
