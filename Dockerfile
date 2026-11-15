FROM python:3.12-slim
WORKDIR /app
COPY server.py .
RUN mkdir -p /data && useradd -u 10001 -m app && chown app:app /data
USER app
ENV HOST=0.0.0.0 PORT=8080 RRR_DB=/data/tasks.db
EXPOSE 8080
CMD ["python","-u","server.py"]
