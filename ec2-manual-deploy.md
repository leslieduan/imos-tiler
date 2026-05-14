# EC2 Manual Deployment Guide

## Step 1 — Launch EC2 Instance (AWS Console)

1. Go to **EC2 → Launch Instance**
2. Choose **Amazon Linux 2023 AMI**
3. Instance type: `t3.medium` recommended — slice cache (100 entries ≈ 700 MB) + processed cache + Docker overhead totals ~1.3 GB at peak, leaving little headroom on `t3.small` (2 GB) under concurrent load
4. Key pair: create or use existing `.pem`
5. **Storage**: increase root volume to **30 GB** — the disk slice cache uses up to `DISK_CACHE_LIMIT_GB` (default 20 GB) on top of the OS and Docker images (~6 GB). The default 8 GB root volume is not enough.
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
git clone https://github.com/your-username/titiler-project.git /app/titiler-project
cd /app/titiler-project
```

> If the repo is private, use a [GitHub personal access token](https://github.com/settings/tokens):
>
> ```bash
> git clone https://your-token@github.com/your-username/titiler-project.git /app/titiler-project
> ```

---

## Step 5 — Pre-load Default Products (Optional)

On first start the container automatically creates `data/products.json` (empty `[]`) and `data/colormaps.json` (empty `{}`) if they don't exist. No manual setup is required.

To start with the default products already registered instead of an empty product list:

```bash
mkdir -p /app/titiler-project/data
cat > /app/titiler-project/data/products.json << 'EOF'
[
  {"id":"sea_level_anomaly","source_path":"s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/","variable":"GSLA","chunk_px":[240,192],"padding":1},
  {"id":"ocean_current","source_path":"s3://aodn-cloud-optimised/model_sea_level_anomaly_gridded_realtime.zarr/","variable":["UCUR","VCUR"],"chunk_px":[240,192],"padding":1},
  {"id":"radar_SouthAustraliaGulfs_wind_delayed_qc_wdir","source_path":"s3://aodn-cloud-optimised/radar_SouthAustraliaGulfs_wind_delayed_qc.zarr","variable":"WDIR","chunk_px":[240,192],"padding":1},
  {"id":"satellite_austemp_heatwave_8day_ssta","source_path":"s3://aodn-cloud-optimised/satellite_austemp_heatwave_8day.zarr","variable":"ssta","chunk_px":[240,192],"padding":1}
]
EOF
```

---

## Step 6 — Set Environment Variables

The Zarr stores are on a public S3 bucket so no AWS credentials are needed. All variables below have working defaults — only `ADMIN_API_KEY` is required.

```bash
cat > /app/titiler-project/.env << 'EOF'
ADMIN_API_KEY=change-me-before-production

# Store TTL: seconds before a Zarr store is re-opened to pick up newly appended time steps.
STORE_TTL_SECONDS=600

# Thread pool: max concurrent sync route handlers.
THREAD_POOL_SIZE=100

# Slice cache: number of fully-computed (store, date) slices to hold in RAM.
SLICE_CACHE_SIZE=100

# Processed cache: number of resampled LOD grids to hold in RAM.
PROCESSED_CACHE_SIZE=400

# Disk cache: maximum total size before pressure eviction runs.
DISK_CACHE_LIMIT_GB=20

# Disk cache: fraction of limit at which eviction triggers.
DISK_EVICTION_THRESHOLD=0.85

# Disk cache: how many of each product's most-recent available dates to cache.
CACHE_DAYS=30

# Disk cache: thread pool size for parallel prewarm at startup.
PREWARM_WORKERS=4

# Disk cache: seconds between background refresh cycles.
CACHE_REFRESH_INTERVAL_SECONDS=14400
EOF
```

---

## Step 7 — Build and Start

```bash
cd /app/titiler-project
docker compose up -d --build
```

Check it's running:

```bash
docker compose ps
docker compose logs -f app
```

On first start, the server prewarmed the disk cache in the background — logs will show lines like `Disk prewarm written (S3): sea_level_anomaly / 2024-01-15` as it fetches the latest 30 dates per product from S3. With 4 products and `PREWARM_WORKERS=4` this takes ~60s. Subsequent restarts load from disk in ~30s.

---

## Step 8 — Verify

```bash
# From inside EC2
curl http://localhost:8000

# From your browser
curl http://<your-ec2-public-ip>/data_tiles/sea_level_anomaly/2024-01-01/manifest.json
```

---

## Step 9 — Future Updates (re-deploy)

```bash
cd /app/titiler-project
git pull
docker compose up -d --build
```

Both `data/` (products, colormaps) and `slice_cache/` are preserved across redeploys — volume-mounted from the host, not rebuilt with the image.

---

## Quick Reference

| What                        | Command                                              |
| --------------------------- | ---------------------------------------------------- |
| View logs                   | `docker compose logs -f`                             |
| Stop                        | `docker compose down`                                |
| Restart                     | `docker compose restart`                             |
| Rebuild after code change   | `docker compose up -d --build`                       |
| Check disk cache size       | `du -sh /app/titiler-project/slice_cache`             |
| Clear disk cache            | `rm -rf /app/titiler-project/slice_cache/*`           |
| Check disk usage            | `df -h`                                              |
