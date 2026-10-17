FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY hub ./hub
RUN pip install --no-cache-dir .
ENV HOVERCAM_STATE_DIR=/data
VOLUME ["/data"]
CMD ["hovercam-hub"]
