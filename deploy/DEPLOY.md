# Live server deployment

Deploys the trading-algo stack (Postgres + algo loop + FastAPI + Next.js) behind an HTTPS
reverse proxy on your VPS, with the dashboard firewalled to your own IP. A server gives you a
**static IP to whitelist with Kotak** and keeps the loop running through market hours.

Target: Ubuntu 22.04+ VPS with a static public IP. Replace `SERVER_IP`, `YOUR_IP`, and
`algo.example.com` with real values.

## 1. Whitelist the server IP with Kotak

Register the server's public IP (`SERVER_IP`) with Kotak Neo (developer portal / support), the
same way your current IP is authorised. Live API calls are rejected from non-whitelisted IPs.

## 2. Install Docker on the server

```bash
ssh ubuntu@SERVER_IP
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out/in so the group applies
```

## 3. Get the code

```bash
git clone <this repo's SSH URL> ~/trading-algo
cd ~/trading-algo
```

## 4. Create `.env` on the server (never commit it)

Copy your working local `.env` and add the production lines below. Generate a strong
`WEB_AUTH_SECRET` (`openssl rand -hex 32`).

```dotenv
# --- production access ---
BIND_ADDR=127.0.0.1:                 # bind api/web to localhost only (proxy fronts them)
SITE_ADDRESS=algo.example.com        # your domain -> real TLS. Or ":443" for a self-signed cert.
NEXT_PUBLIC_API_BASE=                # leave EMPTY: the web app calls the API same-origin via the proxy

# --- must be strong on a public server ---
WEB_AUTH_PASSWORD=<a long random password>
WEB_AUTH_SECRET=<openssl rand -hex 32>

# --- live trading (only when you intend real orders) ---
ALGO_MODE=live
ALGO_CONFIRM_LIVE=YES
INSTALL_BROKER=1                     # bake the Kotak SDK into the algo image
```

### IP only (no domain) — generate a self-signed cert

Clients don't send SNI for a bare IP, so Caddy needs an explicit cert (with the IP in its SAN):

```bash
cd ~/trading-algo && mkdir -p deploy/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout deploy/certs/key.pem -out deploy/certs/cert.pem \
  -subj "/CN=SERVER_IP" -addext "subjectAltName=IP:SERVER_IP"
```

Set `SITE_ADDRESS=:443` in `.env`. The `deploy/Caddyfile` already points at these cert files.
(With a domain instead: set `SITE_ADDRESS=algo.example.com`, delete the `tls` line in the
Caddyfile, and Caddy auto-provisions a real Let's Encrypt cert.)

## 5. Firewall — restrict the dashboard to your IP

Host-level with ufw (SSH stays open; key-only auth):

```bash
sudo ufw allow OpenSSH
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw --force enable
```

**Two ufw + Docker gotchas** handled by the script below:
1. Docker-published ports (Caddy's 80/443) go into the `nat` table and **BYPASS ufw** — a
   `ufw allow 443` rule does NOT restrict them. They must be gated in the `DOCKER-USER` chain.
2. Enabling ufw makes its `FORWARD` chain **reject Docker's forwarded traffic**, so containers
   lose outbound internet (the algo can't reach Kotak). Fix: `DEFAULT_FORWARD_POLICY="ACCEPT"`
   plus explicit `FORWARD` ACCEPT for the Docker bridges (outbound + return only, so the inbound
   80/443 restriction is preserved).

```bash
sudo sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
sudo tee /etc/docker-user-firewall.sh >/dev/null <<'EOF'
#!/bin/bash
ALLOW=YOUR_IP
# INBOUND: restrict Docker-published 80/443 to the operator IP (DOCKER-USER bypasses ufw)
for r in "-j DROP" "-s 127.0.0.1 -j RETURN" "-s $ALLOW -j RETURN"; do
  while iptables -D DOCKER-USER -p tcp -m multiport --dports 80,443 $r 2>/dev/null; do :; done
done
iptables -I DOCKER-USER -p tcp -m multiport --dports 80,443 -j DROP
iptables -I DOCKER-USER -p tcp -m multiport --dports 80,443 -s 127.0.0.1 -j RETURN
iptables -I DOCKER-USER -p tcp -m multiport --dports 80,443 -s $ALLOW -j RETURN
# FORWARD: allow container bridge OUTBOUND + established RETURN (NEW inbound still hits DOCKER-USER)
for r in "-i br+ -j ACCEPT" "-i docker0 -j ACCEPT" "-m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"; do
  while iptables -D FORWARD $r 2>/dev/null; do :; done
done
iptables -I FORWARD -i br+ -j ACCEPT
iptables -I FORWARD -i docker0 -j ACCEPT
iptables -I FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
EOF
sudo chmod +x /etc/docker-user-firewall.sh
sudo tee /etc/systemd/system/docker-user-firewall.service >/dev/null <<'EOF'
[Unit]
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/etc/docker-user-firewall.sh
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now docker-user-firewall.service
```

> If you enabled ufw before adding the FORWARD rules and containers went offline, run
> `sudo systemctl restart docker && sudo systemctl start docker-user-firewall` to restore.

`BIND_ADDR=127.0.0.1:` also keeps ports 8000/3001 on localhost only, so they are never reachable
from the internet. Your public IP is dynamic — if it changes, update `ALLOW` in the script (and
re-run it) and the dashboard will be reachable again; SSH stays open so you can always get back in.

## 6. Build and start

```bash
cd ~/trading-algo
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

Verify:

```bash
docker compose ps                      # db, algo, api, web, caddy all up
docker compose logs -f algo | grep -E "kotak_login_ok|running|session_started"
curl -sk https://localhost/health      # {"status":"ok"} through the proxy
```

Open `https://algo.example.com` (or `https://SERVER_IP`) from your allowlisted IP and log in.

## 7. Updating

```bash
cd ~/trading-algo && git pull
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
```

## Notes / safety

- The algo boots into a live session automatically (mode=live, confirm=YES). To bring it up
  **without** trading, start with `ALGO_MODE=paper`, confirm the dashboard, then switch to live.
- The API's `/api/login` is only reachable from your IP behind the firewall; if you later open
  access wider, add rate-limiting there first.
- Postgres is not published; back up the `pgdata` volume if you care about history.
- Keep `SITE_ADDRESS`, `BIND_ADDR`, and secrets **only** in the server `.env` (gitignored).
