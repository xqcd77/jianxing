FROM python:3.12-slim
RUN useradd --uid 65534 --non-unique --no-create-home --shell /usr/sbin/nologin bankanalysis
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-I", "/input/runner.py"]
