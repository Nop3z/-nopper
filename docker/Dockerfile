# nopper agent image for TSecBench hosted mode ( Throne Protocol qualifier ).
#
# Match rules (2026-10-17/18 online round):
#   - ONE submission attempt, 8h task limit, 50 challenges / 100 flags
#   - ranked by score, then elapsed time, then TOKEN USAGE
#   - no hints; model rate-limits are NOT grounds for a retry
#   - logs/trace are NOT viewable -> the image must fail loud, never hang
#
# Build:  docker build -f docker/Dockerfile -t nopper-agent:latest .
# Export: bash docker/build.sh          ->  dist/nopper-agent.tar.gz
FROM kalilinux/kali-rolling:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/agent/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    NOPPER_MODEL=glm-5.3-flash[1m] \
    NOPPER_TOTAL_MINUTES=470 \
    NOPPER_MINUTES_PER_CHALLENGE=9 \
    NOPPER_MAX_STEPS=10 \
    NOPPER_RETRY_MODEL=3 \
    NOPPER_ROOT=/work/runs

# --- offensive tooling (kali repo, curated to stay < 3GB) -------------------
# Pin a reliable mirror: the default http.kali.org redirect can land on
# mirrors that fail to resolve in build sandboxes.
RUN echo 'deb http://mirrors.tuna.tsinghua.edu.cn/kali kali-rolling main contrib non-free non-free-firmware' \
        > /etc/apt/sources.list \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip file xxd less vim \
        procps psmisc lsof tcpdump iproute2 iputils-ping net-tools \
        python3 python3-pip \
        nodejs npm \
        netcat-traditional socat \
        dnsutils whois \
        openssl \
        nmap masscan \
        whatweb wafw00f nikto \
        gobuster ffuf dirb wfuzz \
        sqlmap \
        hydra \
        binwalk steghide exiftool \
        libgmp-dev libmpfr-dev libmpc-dev \
        redis-tools default-mysql-client postgresql-client \
        gdb checksec \
        smbclient smbmap \
        ldap-utils \
        enum4linux \
        responder impacket-scripts \
        proxychains4 \
        seclists \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && du -sh /usr/share/seclists \
    && rm -rf /usr/share/seclists/IOCs \
              /usr/share/seclists/Misc /usr/share/seclists/Username_Lists \
              /usr/share/seclists/Discovery/DNS \
    && rm -rf /usr/share/doc /usr/share/man /usr/share/vim/vim*/doc \
    && du -sh /usr/share/seclists

# nuclei (Go release) — CVE-template runner referenced by the kb.
# The zip is downloaded on the host (proxy) and shipped via build context:
# the build sandbox cannot reach github.
COPY docker/nuclei.zip /tmp/nuclei.zip
RUN unzip -o /tmp/nuclei.zip -d /usr/local/bin nuclei \
    && chmod +x /usr/local/bin/nuclei && rm -f /tmp/nuclei.zip

# --- model CLI --------------------------------------------------------------
# --allow-scripts: kali's npm blocks postinstall by default, but claude-code
# ships a native binary that its postinstall (node install.cjs) must fetch.
RUN npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code \
    && claude --version \
    && npm cache clean --force

# --- python extras ----------------------------------------------------------
RUN pip3 install --no-cache-dir --break-system-packages \
        requests pyjwt "unicorn==2.0.1.post1" pwntools \
        pycryptodome sympy ROPgadget ropper
# heavy/compiled extras: failure here must not kill the build (kb mentions them
# as optional; stdlib+sympy covers most crypto)
RUN pip3 install --no-cache-dir --break-system-packages gmpy2 z3-solver \
        || echo "WARN: gmpy2/z3 unavailable — continuing without them"

# --- nopper -----------------------------------------------------------------
COPY lib/        /opt/nopper/lib/
COPY templates/  /opt/nopper/templates/
COPY kb/         /opt/nopper/kb/
COPY README.md LICENSE SKILL.md /opt/nopper/

# --- non-root agent user ----------------------------------------------------
# Claude Code refuses --dangerously-skip-permissions as root; run unprivileged
# with a writable HOME for the model CLI's state.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.local/bin /work/runs \
    && ln -sf /opt/nopper/lib/nopper.py /home/agent/.local/bin/nopper \
    && chmod +x /opt/nopper/lib/nopper.py \
    && chown -R agent:agent /work /opt/nopper /home/agent

COPY docker/entrypoint.sh /usr/local/bin/nopper-entrypoint
RUN chmod +x /usr/local/bin/nopper-entrypoint

USER agent
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/nopper-entrypoint"]
