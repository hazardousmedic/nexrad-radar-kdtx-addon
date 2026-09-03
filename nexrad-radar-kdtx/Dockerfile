FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-dev \
    libproj-dev \
    proj-bin \
    proj-data \
    && rm -rf /var/lib/apt/lists/*

# HA add-on persistent storage is /data - pointing HOME there means
# cartopy's Natural Earth cache and MetPy's shapefile cache (normally
# under ~/.local/share and ~/.cache) land in /data too, with no extra
# bind mounts needed, and survive add-on rebuilds/updates.
ENV HOME=/data

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/fetch_render.py app/entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8600
CMD ["./entrypoint.sh"]
