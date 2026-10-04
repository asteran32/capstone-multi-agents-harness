FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==9.1.1
COPY runner.py /runner.py
USER 65534:65534
WORKDIR /tmp
CMD ["python", "/runner.py"]
