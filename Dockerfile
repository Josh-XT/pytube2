# Use Python 3.9 as the base image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install dependencies (ffmpeg for video processing if needed)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the pytube2 library files
COPY . /app/pytube2/

# Install the library
WORKDIR /app/pytube2
RUN pip install -e .

# Create a directory for downloads
RUN mkdir -p /downloads

# Set the working directory for running commands
WORKDIR /downloads

# Set entrypoint to pytube command
ENTRYPOINT ["pytube"]
# Default command shows help
CMD ["--help"]
