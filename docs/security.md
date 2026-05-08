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

nginx sits in front of the app and is the **only publicly exposed service** (port 80). The app port (8000) is never published outside the Docker network — only the nginx container can reach it.

nginx handles two cases:

```
Internet → port 80 → nginx ─┬─ /admin     → 403 blocked
                             └─ /tiles, /  → proxied to app:8000
```

Any request to `/admin` arriving through port 80 is rejected by nginx before it ever reaches FastAPI. Regular tile requests are forwarded normally.

### Layer 3: EC2 Security Group

The EC2 Security Group is the network-level firewall. Port 8000 has no inbound rule, so it is unreachable from the internet even if nginx were misconfigured.

| Port | Source | Purpose |
|------|--------|---------|
| 80 | 0.0.0.0/0 | Public tile requests via nginx |
| 443 | 0.0.0.0/0 | HTTPS (if configured) |
| 22 | your IP | SSH access and admin tunneling |
| 8000 | ❌ none | Must not be publicly exposed |

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

nginx runs on port 80 and blocks `/admin`, but port 8000 is still reachable on your local machine — Docker exposes it within your machine's network even though it is not published to the internet. There is no Security Group or firewall locally, so port 8000 is open.

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

On EC2 the Security Group blocks port 8000 from the internet entirely — unlike local Docker, there is a real AWS-level firewall in place. nginx also blocks `/admin` on port 80. There is no direct path to the admin endpoints from outside.

The only way in is an **SSH tunnel**, which forwards a local port on your machine through the SSH connection (port 22) to port 8000 on the EC2 instance — bypassing both the Security Group and nginx:

```bash
ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip
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

Internet → port 80 → nginx → /admin  ❌ blocked
                            → /tiles  ✅ proxied to app:8000
```

Then call the admin endpoint from your machine (the API key is still required):

```bash
curl http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key"
```

This is why the API key still matters even with the SSH tunnel — nginx protects the public internet path, but the tunnel bypasses nginx. The API key is the last line of defence for anyone with SSH access to the instance.
