# The grading side. Runs the hidden tests and the fake upstreams.
#
# The grader needs the test files, so it runs as root here and the *test tree* is made
# unreadable to everyone else. The agent never enters this container at all; the uid
# separation that matters is inside it, between the grader and the model-authored code it
# executes.
FROM python:3.11-slim

RUN pip install --no-cache-dir pytest==9.1.1

RUN useradd --uid 10002 --create-home --shell /usr/sbin/nologin runner \
 && useradd --uid 10003 --create-home --shell /usr/sbin/nologin model \
 && mkdir -p /grading && chmod 0700 /grading

COPY bugbench /srv/bugbench
ENV PYTHONPATH=/srv
WORKDIR /grading
CMD ["python", "-m", "bugbench.services"]
