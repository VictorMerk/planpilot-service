FROM python:3.11-slim

WORKDIR /app

# Install only required build dependencies
RUN apt-get update && \
    apt-get install -y cmake g++ make && \
    rm -rf /var/lib/apt/lists/*

# Copy backend files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Clean any old build artifacts
RUN rm -rf ./lib/downward/builds/release

# Run downward build script
RUN python3 ./lib/downward/build.py

# Expose Flask port
EXPOSE 5000

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]
