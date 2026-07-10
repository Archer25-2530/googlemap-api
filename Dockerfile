FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY maps_mcp ./maps_mcp

ENV PYTHONUNBUFFERED=1
EXPOSE 3000

CMD ["python", "-m", "maps_mcp"]
