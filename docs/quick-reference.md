# ModelFungible — Regular User Quick Reference

> Keep this by your desk. For full documentation, see `docs/enterprise-user-guide.md`

---

## 🔑 Login

**URL:** `https://ai.yourcompany.com/admin`  
**Session lasts:** 12 hours  
**Forgot password?** Ask your administrator to reset it.

---

## 📊 Dashboard

Your home screen — shows system health, model status, and recent activity.

**Green model** = healthy  
**Yellow** = degraded (slow)  
**Red** = circuit breaker open (failing fast)

---

## 📋 Running a Strategy

### Via Admin UI (easiest)
1. **Strategies** tab → find your strategy
2. Click the name to view it
3. Copy the JSON → share with your administrator to deploy

### Via API
```bash
curl -X POST https://ai.yourcompany.com/api/execute \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: YOUR_TOKEN" \
  -d '{
    "strategy_id": "contract_risk",
    "model": "claude-production",
    "context": {"contract_text": "Party A agrees to..."}
  }'
```

---

## 🔍 Checking Your Audit Trail

1. **Audit Logs** tab
2. Set **Actor** = your user ID
3. Set date range if needed
4. Click **Query**

---

## 👤 Understanding Your Role

| Your Role | What You Can Do |
|-----------|----------------|
| **Viewer** | Read-only: dashboard, audit logs |
| **Trader** | + Run strategies, validate strategy JSON |
| **Admin** | + Manage users, register models, system settings |

---

## ⚠️ Common Issues

| Problem | Solution |
|---------|----------|
| "Session expired" | Log out and log back in |
| "403 Forbidden" | You don't have permission — ask admin |
| Model shows red | Circuit breaker open — ask admin to reset |
| Can't see audit logs | Your role may be `viewer` — limited to your own logs |

---

## 🆘 Need Help?

- **Admin questions** → your IT administrator
- **License / billing** → your vendor
- **Bugs** → GitHub Issues

**Admin contact:** _______________________  
**System URL:** _______________________
