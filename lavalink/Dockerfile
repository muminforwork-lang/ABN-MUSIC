FROM ghcr.io/lavalink-devs/lavalink:4.2.2

# Use the standalone yt-dlp binary so LavaSrc can attempt a broad range of public URLs.
# Pin and verify the release asset at build time rather than depending on an OS Python/pip version.
USER root
ARG YT_DLP_VERSION=2026.08.19
ARG YT_DLP_LINUX_SHA256=58162f9bfdc27458ea47bfcb311cf47028f17d8154a8bf7d689861d46399230a
RUN apt-get update     && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg     && curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/download/${YT_DLP_VERSION}/yt-dlp_linux" -o /usr/local/bin/yt-dlp     && echo "${YT_DLP_LINUX_SHA256}  /usr/local/bin/yt-dlp" | sha256sum -c -     && chmod 0755 /usr/local/bin/yt-dlp     && /usr/local/bin/yt-dlp --version     && apt-get clean     && rm -rf /var/lib/apt/lists/* /root/.cache

COPY application.yml /opt/Lavalink/application.yml
USER 322:322
