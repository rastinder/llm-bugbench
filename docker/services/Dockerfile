FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
 && rm -rf /var/lib/apt/lists/*

# Add hermetic adb dummy script that serves 1x1 png for screencap
RUN printf '#!/bin/sh\npython3 -c "import sys; sys.stdout.buffer.write(b\"\\x89PNG\\r\\n\\x1a\\n\\x00\\x00\\x00\\rIHDR\\x00\\x00\\x00\\x01\\x00\\x00\\x00\\x01\\x08\\x06\\x00\\x00\\x00\\x1f\\x15c4\\x00\\x00\\x00\\rIDATx\\x9cc\\x60\\x00\\x00\\x00\\x02\\x00\\x01H\\xaf\\xa4q\\x00\\x00\\x00\\x00IEND\\xaeB\\x60\\x82\")"\n' > /usr/local/bin/adb \
 && chmod +x /usr/local/bin/adb

RUN pip install --no-cache-dir pytest==9.1.1 pydantic-settings pyyaml requests httpx

WORKDIR /srv
COPY data /srv/data
COPY sandbox.py /srv/sandbox.py
COPY server.py /srv/server.py

EXPOSE 8000 5000 8888
CMD ["python3", "/srv/server.py"]
