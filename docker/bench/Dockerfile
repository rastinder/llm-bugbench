# The model's workspace. Deliberately minimal: no pytest, no build tooling, nothing that
# could be used to introspect the grading environment. Anything the agent needs it must
# ask for through the API, which is the only channel it has.
FROM python:3.11-slim

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bench \
 && mkdir -p /work && chown 10001:10001 /work

COPY --chown=10001:10001 grpc /work/grpc
USER 10001
WORKDIR /work

# No network tools are installed on purpose: an agent that can neither reach out nor
# inspect the grader has fewer ways to cheat than one that merely is told not to.
CMD ["sleep", "infinity"]
