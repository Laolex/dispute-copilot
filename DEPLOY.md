# Deploy

The app serves a password-protected page that can send PayPal actions. Use **sandbox credentials only**, and
keep it off any machine that runs live money or trading services.

## Option A: Render (recommended; this is what the demo URL should be)

`render.yaml` describes the service: a Docker web service, a 1 GB persistent disk for the SQLite database and
audit log, a health check, and secrets entered in the dashboard.

1. **Public repo on GitHub** with this code (the hackathon requires a public, open-source repo anyway).
2. **Claim the hackathon credits** from the Render link on the Devpost sponsor page.
3. In Render: **New > Blueprint**, pick the repo. It reads `render.yaml` and asks for:
   - `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` (sandbox app credentials).
   - `DC_ADMIN_PASSWORD` is generated for you; read it later under the service's **Environment** tab.
4. First deploy builds the image. When it is live, check `https://<service>.onrender.com/healthz` returns `{"ok":true}`
   and that `/` asks for a password (any username, the generated password).
5. **Register the webhook** against the live URL, from any machine with sandbox creds exported:
   ```bash
   python -m spike.spike webhook-create --url https://<service>.onrender.com/webhooks/paypal --yes
   ```
   Add the printed id as `PAYPAL_WEBHOOK_ID` in the Environment tab (Render redeploys).
6. Optional: add `ANTHROPIC_API_KEY` in the Environment tab to switch from the offline fallback to the model.

Notes
- The plan is `starter` because a persistent disk needs a paid instance (the Blueprint reference does not say which
  plans support disks, so check this in the dashboard if creation complains). Without a disk the audit log would
  vanish on every redeploy.
- `autoDeploy` is off. Deploy deliberately from the dashboard so a late bad push cannot take the demo down.
- Merchant records (`data/orders.json`) and evidence files (`data/docs/`) are baked into the image. To change
  them, commit and deploy.
- A service with an attached disk cannot be scaled to several instances. One instance is what this app wants.

## Option B: your own server (Docker + Caddy)

Use an isolated small instance (for example Ubuntu 24.04 on EC2 or Lightsail, ports 80/443 open, SSH from your IP only).

    curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER   # log out/in
    git clone <repo> dispute-copilot && cd dispute-copilot
    cp deploy.env.example deploy.env        # fill in; chmod 600 deploy.env
    echo "DOMAIN=your.domain.example" > .env   # a free name: <ip-with-dashes>.sslip.io
    docker compose up -d --build
    curl https://your.domain.example/healthz

Then register the webhook as above, put the id in `deploy.env`, and `docker compose up -d`.
Caddy obtains the HTTPS certificate automatically. Data in `./data` is mounted, so edits to `orders.json` and
`data/docs/` apply without a rebuild.

## Operate
- Logs: Render dashboard (Logs tab) or `docker compose logs -f app`.
- The audit page shows whether the hash chain is intact; a broken chain is shown in red on every page.
- Rotate the admin password by changing the environment variable and redeploying.
- Back up the database file from the disk (`/var/lib/dc/dispute_copilot.db`) before any risky change.
