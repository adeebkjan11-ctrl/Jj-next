# Deploying Lazy Farmers on Railway

Railway builds this repo with Nixpacks: `requirements.txt` for the dependencies, `Procfile` for the
start command. No Dockerfile, no shell scripts, no CLI access needed — everything is managed from the
web dashboard.

## Steps

1. Push the repo to GitHub, then in Railway: **New Project -> Deploy from GitHub repo**.
   `railway.json` pins the Nixpacks builder and `Procfile` runs `python neura.py`.
2. **Add a volume** (Service -> Settings -> Volumes) with any mount path, e.g. `/data`, and set
   `LAZYFARMERS_DATA_ROOT` to that path. On first boot the app copies the shipped config defaults
   there and keeps `config/` and `data/` on the volume. Without it, accounts, tokens, dashboard
   credentials and stats are wiped on every redeploy.
3. Set service variables:
   | Variable | Purpose |
   | --- | --- |
   | `LAZYFARMERS_DASHBOARD_USER` | Dashboard login user |
   | `LAZYFARMERS_DASHBOARD_PASSWORD` | Dashboard login password - **set this**, the shipped default is public |
   | `LAZYFARMERS_DATA_ROOT` | Volume mount path, e.g. `/data` |
   | `LAZYFARMERS_HEADLESS` | Optional, forces headless mode; auto-detected when there is no terminal |

   `PORT` is injected by Railway and used by the dashboard (falls back to 8000 locally).
4. Generate a domain (Settings -> Networking) and log in.

## Everything is on the Accounts page

The interactive terminal menu is not reachable on Railway, so the dashboard does all of it:

- **Add Account** - name, token, optional channel IDs, optional proxy, enabled toggle.
- **Bulk Import** - paste one token per line; manual channel IDs are optional. Accounts are named
  `acc1`, `acc2`, ... (or your own prefix).
- **Monitor Bot** - save and validate a separate bot token, choose a server, then create
  and assign one private channel per three accounts before starting them.
- **Verify** - logs the token in, reports the Discord username and keeps only the channel IDs the
  account can actually see. Available per account or for all enabled accounts.
- **Start / Stop** - connects or disconnects that account while the service keeps running, so a newly
  added account needs no redeploy. **Start All** launches every enabled account, **Stop All**
  disconnects everything.
- **Edit / Del** - change name, token, channels, proxy, or remove the account.
- Each row shows `RUNNING` or `STOPPED`, refreshed every 5 seconds.

Accounts remain stopped after a boot or redeploy. Start them explicitly from the dashboard. A configured monitor bot reconnects independently.

Settings (commands, stealth, gambling, security, owner triggers) are edited on the **Config** page,
proxies on the **Proxies** page, captchas on the **Security** page.

## Notes

- Keep replicas at **1**. Two replicas means every account runs commands twice, which is an instant
  ban pattern.
- `nixpacks.toml` adds `git` (needed to install the pinned `discord.py-self` commit) and `libgomp1`
  (needed by `onnxruntime`). If a build ever ignores it, set `NIXPACKS_PKGS=git` and
  `NIXPACKS_APT_PKGS=libgomp1` as service variables instead.
- Captchas: configure a solver service key on the Security page, or solve them manually from the
  dashboard captcha panel. Desktop notifications and beeps do nothing on a server - use the security
  webhook instead.
- Selfbots violate Discord's ToS and OwO's rules (see the README warning). Hosting on Railway does
  not reduce that risk, and public dashboards get scanned - use a strong dashboard password.
