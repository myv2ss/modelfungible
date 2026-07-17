# ModelFungible — Administrator Quick Start

> First 15 minutes: everything you need to get the system production-ready.

---

## ⏱️ First-Time Setup (15 min)

### 1. Change admin password NOW
```bash
export MODELFUNGIBLE_ADMIN_PASSWORD="YourStrongPassword!@#$"
```
Or: Admin UI → Compliance tab → update `admin` user.

### 2. Install your license
```bash
export MODELFUNGIBLE_LICENSE_KEY="MODEL-xxxxxxxxxxxx.your_sig"
export MODELFUNGIBLE_LICENSE_SECRET="your_secret"
```
Or: Admin UI → Compliance tab → License Status.

### 3. Set your retention policy
```bash
export MODELFUNGIBLE_RETENTION_POLICY="gdpr"    # 30 days (EU)
export MODELFUNGIBLE_RETENTION_POLICY="hipaa"    # 6 years (healthcare)
export MODELFUNGIBLE_RETENTION_POLICY="finra"    # 6 years (finance)
```

### 4. Add your AI models
Admin UI → **Deployments** tab → **+ Add Model**

Required per model:
- Name (internal identifier)
- Provider (openai / anthropic / groq / ollama / vertexai)
- Model ID (exact name on provider)
- API Key
- p50 Latency (ms) — helps with load balancing

### 5. Create user accounts for your team
Admin UI → Compliance tab → Users → Add User

Give each person their own account. Never share admin credentials.

---

## 🔄 Daily Operations

### View system health
Dashboard tab — check model health, circuit breakers, today's call volume.

### Reset a circuit breaker
Dashboard → Circuit Breakers → Reset (next to affected model).

### Review audit logs
Audit Logs tab — verify chain integrity weekly: click **Verify Chain**.

### Onboard a new user
1. Compliance → Users → Add User
2. Set role: `viewer` (read-only), `trader` (run strategies), `admin` (full access)
3. Share their login credentials securely

---

## 🔒 Security Checklist

- [ ] Changed default admin password
- [ ] Installed commercial license
- [ ] Set retention policy for your regulation
- [ ] Created individual accounts for all users
- [ ] Enabled HTTPS on the admin UI (production)
- [ ] Verified audit chain integrity
- [ ] Reviewed user list — removed unused accounts

---

## 🚀 Production Deployment

```bash
# Run as systemd service
sudo tee /etc/systemd/system/modelfungible-admin.service > /dev/null <<EOF
[Unit]
Description=ModelFungible Enterprise Admin
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/modelfungible
ExecStart=/usr/bin/python3 -m modelfungible.enterprise.admin_app
Environment=MODELFUNGIBLE_ADMIN_PASSWORD=CHANGE_ME
Environment=MODELFUNGIBLE_LICENSE_KEY=YOUR_KEY
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable modelfungible-admin
sudo systemctl start modelfungible-admin
```

**Production tips:**
- Always use HTTPS (put behind nginx/Caddy with TLS)
- Set `MODELFUNGIBLE_AUDIT_DIR` to a persistent location (not `/tmp`)
- Monitor disk space — audit logs grow with usage
- Set up log rotation for the audit directory

---

## 📁 Key Files & Paths

| Item | Default Path |
|------|-------------|
| Audit logs | `/tmp/modelfungible_audit/` |
| Strategy examples | `modelfungible/examples/strategies/` |
| Config env vars | See user guide Section 3.5 |

---

## 🆘 Troubleshooting

| Symptom | Fix |
|---------|-----|
| Can't login | Check password; ask another admin to reset |
| Model won't register | Name already used — try a different name |
| Audit verify fails | Tampering detected — escalate to security team |
| High latency | Check provider status; try a different model |
| 403 on admin endpoints | Your account role doesn't have admin access |

---

## 📚 Full Documentation

`docs/enterprise-user-guide.md` — complete admin + user guide
`docs/quick-reference.md` — regular user desk reference card
