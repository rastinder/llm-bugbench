FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

COPY opencode /usr/local/bin/opencode
RUN chmod +x /usr/local/bin/opencode

RUN mkdir -p /root/.config/opencode
COPY opencode.json /root/.config/opencode/opencode.json
COPY agent.py /srv/agent.py

WORKDIR /work
CMD ["python3", "/srv/agent.py", "--help"]
