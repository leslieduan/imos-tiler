# EC2 Manual Deployment Guide

## Step 1 — Launch EC2 Instance (AWS Console)

1. Go to **EC2 → Launch Instance**
2. Choose **Amazon Linux 2023 AMI**
3. Instance type: `t3.medium` recommended — slice cache (100 entries ≈ 700 MB) + processed cache + Docker overhead totals ~1.3 GB at peak, leaving little headroom on `t3.small` (2 GB) under concurrent load
4. Key pair: create or use existing `.pem`
5. **Storage**: default 8 GB root volume is fine — there is no on-disk cache; the server keeps everything in RAM and disappears on restart. The only persistent footprint is the OS and Docker images (~6 GB).
6. Security group — open these ports:
   - **22** (SSH) — anywhere (`0.0.0.0/0`)
   - **80** (HTTP) — anywhere (`0.0.0.0/0`)
7. Launch

---

## Step 2 — SSH into the Instance

```bash
ssh -i titiler-demo-key.pem ec2-user@<your-ec2-public-ip>
```

---

## Step 3 — Install Docker

```bash
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user

# Install Docker Compose plugin (architecture-aware)
DOCKER_COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
ARCH=$(uname -m)  # x86_64 or aarch64
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Install latest Docker Buildx (compose build requires 0.17.0+)
BUILDX_VERSION=$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
BUILDX_ARCH=$([ "$ARCH" = "aarch64" ] && echo "arm64" || echo "amd64")
mkdir -p ~/.docker/cli-plugins
curl -SL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${BUILDX_ARCH}" \
  -o ~/.docker/cli-plugins/docker-buildx
chmod +x ~/.docker/cli-plugins/docker-buildx

# Apply group change without re-login
newgrp docker
```

Verify:

```bash
docker --version
docker compose version
docker buildx version
```

---

## Step 4 — Clone the Project

```bash
# Create /app directory and clone
sudo mkdir -p /app
sudo chown ec2-user:ec2-user /app
git clone https://github.com/your-username/imos-tiler.git /app/imos-tiler
cd /app/imos-tiler
```

> If the repo is private, use a [GitHub personal access token](https://github.com/settings/tokens):
>
> ```bash
> git clone https://your-token@github.com/your-username/imos-tiler.git /app/imos-tiler
> ```

---

## Step 5 — Products and Colormaps (Optional)

Products and custom colormaps are static config committed with the code — `src/app/config/products.json` and `src/app/config/colormaps.json` — baked into the Docker image at build time. No runtime setup is required; the clone in Step 4 already has a working default product set.

To deploy a different product mix on this instance, edit `src/app/config/products.json` before building:

```bash
cat > /app/imos-tiler/src/app/config/products.json << 'EOF'
[
  {"id":"sea_level_anomaly","source_path":"s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/","variable":"GSLA","chunk_px":[240,192],"padding":1},
  {"id":"ocean_current","source_path":"s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/","variable":["UCUR","VCUR"],"chunk_px":[240,192],"padding":1},
  {"id":"satellite_austemp_heatwave_8day_ssta","source_path":"s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr","variable":"ssta","chunk_px":[240,192],"padding":1}
]
EOF
```

`docker compose up --build` (Step 7) picks up the change — the file is copied into the image, not bind-mounted.

---

## Step 6 — Set Environment Variables

The Zarr stores are on a public S3 bucket so no AWS credentials are needed. All variables below have working defaults, so a `.env` file is optional.

```bash
cat > /app/imos-tiler/.env << 'EOF'
# Timezone used to convert UTC store timestamps to local dates for the manifest and tile endpoints.
# Set to any IANA timezone name (e.g. America/New_York, Europe/London) to deploy for a different region.
# Both get_available_dates and load_slice must use the same value — do not change one without the other.
TILE_TIMEZONE=Australia/Sydney

# Store TTL: seconds before a Zarr store is re-opened to pick up newly appended time steps.
STORE_TTL_SECONDS=600

# Thread pool: max concurrent sync route handlers.
THREAD_POOL_SIZE=100

# Cache backend for L1 (processed grid) / L2 (slice): "none" disables caching
# entirely; "redis" shares a cache across instances via REDIS_URL.
CACHE_BACKEND=none

# Slice cache (L2): per-entry TTL in seconds, only used when CACHE_BACKEND=redis.
SLICE_CACHE_TTL_SECONDS=600

# Processed cache (L1): per-entry TTL in seconds, only used when CACHE_BACKEND=redis.
PROCESSED_CACHE_TTL_SECONDS=600

# /animation: per-frame S3 fan-out concurrency cap.
ANIMATION_WORKERS=10
EOF
```

---

## Step 7 — Build and Start

```bash
cd /app/imos-tiler
docker compose up -d --build
```

Check it's running:

```bash
docker compose ps
docker compose logs -f app
```

On first start, the server prewarms each product's Zarr store *metadata* in the background (no slice data) — logs will show one `Store opened` line per unique store URL. This is quick (metadata only, no data chunks) and every restart pays it again, since nothing is cached to disk. The first tile request for each `(product, date)` still pays a full cold S3 fetch (~2s for a satellite-class slice).

---

## Step 8 — Verify

```bash
# From inside EC2
curl http://localhost:8000/health

# From your browser
curl http://<your-ec2-public-ip>/health
curl http://<your-ec2-public-ip>/data_tiles/products
```

---

## Step 9 — Future Updates (re-deploy)

```bash
cd /app/imos-tiler
git pull
docker compose up -d --build
```

Nothing is preserved across a redeploy — there is no on-disk cache and no bind-mounted state. Every restart starts fully cold; a product/colormap change takes effect by editing `src/app/config/{products,colormaps}.json`, committing, and rebuilding.

---

## Quick Reference

| What                      | Command                                     |
| ------------------------- | ------------------------------------------- |
| View logs                 | `docker compose logs -f`                    |
| Stop                      | `docker compose down`                       |
| Restart                   | `docker compose restart`                    |
| Rebuild after code change | `docker compose up -d --build`              |
| Check disk usage          | `df -h`                                     |
