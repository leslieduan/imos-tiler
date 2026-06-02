# Security

## Admin endpoint protection

The `/admin` endpoints (add/remove products) are protected by three layers working together.

### Layer 1: API key

All `/admin` routes require an `X-Admin-Key` header. The expected value is read from the `ADMIN_API_KEY` environment variable at runtime. Requests with a missing or incorrect key receive a `403` response.

Set the key on the server before starting:

```bash
# create a .env file next to docker-compose.yml (never commit this file)
echo "ADMIN_API_KEY=your-secret-key" > .env
chmod 600 .env
```

Call admin endpoints with the header:

```bash
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"id": "...", "source_path": "...", "variable": "..."}'
```

### Layer 2: nginx blocks `/admin` from the public internet

nginx sits in front of the app and is the **only publicly exposed service** (ports 80 and 443). The app port (8000) is bound to `127.0.0.1` on the host — it is not reachable from any external network interface, only from localhost.

nginx terminates TLS on port 443 and redirects plain HTTP to it:

```
Internet → port 80  → nginx → 301 redirect → https://…
Internet → port 443 → nginx (TLS) ─┬─ /admin     → 403 blocked
                                    └─ /tiles, /  → proxied to app:8000
```

Any request to `/admin` is rejected by nginx before it ever reaches FastAPI. Regular tile requests are forwarded normally over the internal Docker network as plain HTTP (see [Transport security](#transport-security-httpstls) below).

### Layer 3: EC2 Security Group

The EC2 Security Group is the network-level firewall. Port 8000 has no inbound rule, so it is unreachable from the internet even if nginx were misconfigured.

| Port | Source    | Purpose                                                     |
| ---- | --------- | ----------------------------------------------------------- |
| 443  | 0.0.0.0/0 | Public tile requests over HTTPS via nginx                   |
| 80   | 0.0.0.0/0 | Redirects to 443 (keep open so the redirect works)          |
| 22   | 0.0.0.0/0 | SSH access and admin tunneling (key-based auth protects it) |
| 8000 | ❌ none   | Must not be publicly exposed                                |

---

## Transport security (HTTPS/TLS)

nginx terminates TLS, so all public traffic is encrypted. Internally, nginx proxies to the app over plain HTTP — that hop stays on the Docker network and the app port is bound to `127.0.0.1`, so nothing untrusted can observe it.

```
client ──HTTPS (encrypted)──► nginx ──HTTP (internal, private)──► app:8000
        TLS terminates here ▲
        (cert + key live here)
```

### Current setup: self-signed certificate (demo)

This deployment uses a **self-signed certificate** because the instance has no domain name. The connection is fully encrypted, but browsers show a one-time *"Your connection is not private"* warning (no Certificate Authority vouches for the cert) — click through to proceed. This is acceptable for a demo only; do not treat the warning as safe to ignore on a production-facing service.

The cert and key are **not committed to git** (`docker/certs/` is gitignored). Generate them on the host before starting the stack:

```bash
mkdir -p docker/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout docker/certs/server.key \
  -out docker/certs/server.crt \
  -subj "/CN=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)"
```

`docker-compose.yml` mounts `./docker/certs` read-only into the nginx container at `/etc/nginx/certs`, where `docker/nginx.conf` references `server.crt` / `server.key`.

Remember to open **inbound port 443** in the EC2 Security Group — TLS config alone does nothing if the port is firewalled.

### Upgrading to a trusted certificate

When this gets a real domain name, replace the self-signed cert with a CA-issued one to remove the browser warning. Two standard paths:

- **Let's Encrypt + certbot** — free, auto-renewing; keep terminating TLS at nginx.
- **AWS ACM behind an ALB or CloudFront** — managed, auto-renewing; TLS terminates at the AWS edge and nginx can drop back to plain HTTP on port 80.

Both require a domain pointed at the instance (or load balancer); neither issues certs for a bare IP address.

---

## Accessing admin endpoints by environment

How you reach `/admin` depends on where the server is running.

### Local (`uv run uvicorn`)

No Docker, no nginx. FastAPI runs directly on port 8000. Call it directly:

```bash
curl http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key"
```

### Local (Docker)

nginx runs on port 80 and blocks `/admin`. Port 8000 is bound to `127.0.0.1` on the host, so it is accessible at `localhost:8000` on your local machine but not from any other machine.

```
localhost:8000 → FastAPI directly  ✅ bypasses nginx
localhost:80   → nginx → /admin    ❌ blocked
```

Call port 8000 directly:

```bash
curl http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key"
```

### EC2 (Docker)

On EC2, port 8000 is protected by two independent layers: it is bound to `127.0.0.1` on the host (so only localhost can reach it, regardless of firewall rules), and the EC2 Security Group has no inbound rule for port 8000. nginx also blocks `/admin` on port 80. There is no direct path to the admin endpoints from outside.

The only way in is an **SSH tunnel**, which forwards a local port on your machine through the SSH connection (port 22) to port 8000 on the EC2 instance — bypassing both the Security Group and nginx:

```bash
# Run this from your local machine, not from inside EC2
ssh -i titiler-demo-key.pem -L 8000:localhost:8000 ec2-user@your-ec2-ip
```

This command has three parts:

```
-L  8000  :  localhost  :  8000
     ↑            ↑          ↑
 local port    where to    port on the
 on your       forward     EC2 instance
 machine       from EC2
```

After running it, `localhost:8000` on your machine routes through SSH to `localhost:8000` on the EC2 instance, hitting FastAPI directly:

```
your machine:8000 ──SSH (port 22)──► EC2 localhost:8000 → FastAPI
                                      (Security Group and nginx never involved)

Internet → port 443 → nginx (TLS) → /admin  ❌ blocked
                                   → /tiles  ✅ proxied to app:8000
```

Then call the admin endpoint from your machine (the API key is still required):

```bash
curl http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key"
```

This is why the API key still matters even with the SSH tunnel — nginx protects the public internet path, but the tunnel bypasses nginx. The API key is the last line of defence for anyone with SSH access to the instance.
