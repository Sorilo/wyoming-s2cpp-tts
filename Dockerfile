# Placeholder Dockerfile for wyoming-s2cpp-tts.
#
# This scaffold intentionally does NOT build s2.cpp, CUDA components, or models.
# Future TODOs:
# - Choose a CUDA-capable base image compatible with Unraid/NVIDIA runtime.
# - Build or copy the s2.cpp server binary.
# - Install the Python Wyoming wrapper dependencies.
# - Expose Wyoming TCP port 10200 and health/debug HTTP port 8088.
# - Keep s2.cpp HTTP port 3030 internal unless debug mode is enabled.
# - Start both s2.cpp and the Python wrapper from entrypoint.sh or a supervisor.

FROM python:3.12-slim

WORKDIR /app
COPY . /app

RUN chmod +x /app/entrypoint.sh

EXPOSE 10200/tcp
EXPOSE 8088/tcp

ENTRYPOINT ["/app/entrypoint.sh"]
