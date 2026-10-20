FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY loader/ loader/

EXPOSE 8765

CMD ["python3", "app/ontotek_ui.py"]
