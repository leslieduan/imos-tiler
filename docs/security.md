# Security

## Admin endpoint protection

The `/admin` endpoints (add/remove products) are protected by two layers.

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

nginx is the only publicly exposed service (port 80). It returns `403` for any request to `/admin` and proxies everything else to the app on port 8000. Port 8000 is never published to the host — only the nginx container can reach it.

```
Internet → port 80 → nginx → /admin blocked
                           → /tiles, / proxied to app:8000
```

### Accessing admin endpoints in production (EC2)

Port 8000 is not exposed publicly. Use an SSH tunnel to forward it to your local machine:

```bash
ssh -L 8000:localhost:8000 ec2-user@your-ec2-ip
```

Then call the endpoint locally (the API key is still required):

```bash
curl -X POST http://localhost:8000/admin/products \
  -H "X-Admin-Key: your-secret-key" \
  ...
```

### EC2 Security Group

Ensure the Security Group has no inbound rule for port 8000. Only ports 80 (HTTP), 443 (HTTPS), and 22 (SSH) should be open.

| Port | Source | Purpose |
|------|--------|---------|
| 80 | 0.0.0.0/0 | Public tile requests via nginx |
| 443 | 0.0.0.0/0 | HTTPS (if configured) |
| 22 | your IP | SSH access and admin tunneling |
| 8000 | ❌ none | Must not be publicly exposed |
