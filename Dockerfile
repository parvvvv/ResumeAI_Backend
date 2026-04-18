FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for Docker cache efficiency
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Let Playwright install Chromium + all required OS-level dependencies automatically
RUN playwright install --with-deps chromium

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Start the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
