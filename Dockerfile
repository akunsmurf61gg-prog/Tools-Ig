# Menggunakan official Python image dengan Playwright pre-installed atau base python
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory di dalam container
WORKDIR /app

# Salin file requirements terlebih dahulu
COPY requirements.txt .

# Install dependencies python
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh file project ke dalam container
COPY . .

# Port yang digunakan FastAPI (Render otomatis mendeteksi port $PORT)
EXPOSE 8000

# Jalankan FastAPI menggunakan Uvicorn
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
