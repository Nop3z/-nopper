# TUI-mode variant: same toolchain as the base image, plus tmux and an
# entrypoint that runs agents as interactive claude sessions in tmux windows
# (driver feeds missions + nudges; sessions stay alive for back-and-forth).
#
# Build:  docker build -f docker/Dockerfile.tmux -t nopper-agent:tmux .
#         (requires nopper-agent:latest to exist locally)
FROM nopper-agent:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends tmux \
    && rm -rf /var/lib/apt/lists/*

COPY docker/tmux-drive.sh      /opt/nopper/docker/tmux-drive.sh
COPY docker/entrypoint-tmux.sh /usr/local/bin/nopper-entrypoint
RUN chmod +x /usr/local/bin/nopper-entrypoint /opt/nopper/docker/tmux-drive.sh
USER agent
