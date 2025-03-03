# Dockerfile
FROM ubuntu:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git redis-server
#Install the packages
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .  # Copy all the files
EXPOSE 8000 #Set the port.
CMD ["python3", "main.py"]