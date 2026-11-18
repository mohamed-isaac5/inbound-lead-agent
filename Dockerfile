FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ila ila
ENV ILA_HOST=0.0.0.0 ILA_DB=/data/ila.db
VOLUME /data
EXPOSE 8765
CMD ["python", "-m", "ila.api"]
