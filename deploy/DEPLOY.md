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

> No domain? Set `SITE_ADDRESS=:443` and uncomment `tls internal` in `deploy/Caddyfile` for a
> self-signed cert (one browser warning; fine for a single-user dashboard). A domain is cleaner —
> point an A record at `SERVER_IP` and set `SITE_ADDRESS` to it.

## 5. Firewall — restrict the dashboard to your IP

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow from YOUR_IP to any port 443 proto tcp   # dashboard, only from your IP
sudo ufw allow from YOUR_IP to any port 80  proto tcp   # ACME http-01 / redirect (domain TLS)
sudo ufw enable
```

`BIND_ADDR=127.0.0.1:` keeps ports 8000/3001 on localhost only, so they are never reachable
from the internet even though Docker's iptables rules would otherwise bypass ufw.

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
