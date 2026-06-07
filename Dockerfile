FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HBEXTRA_DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8000", "wsgi:application"]
