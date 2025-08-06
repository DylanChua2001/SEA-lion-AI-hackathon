FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

# Don't COPY the source code here; Compose mounts it!
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
