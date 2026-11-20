# Auto-deploy to cPanel shared hosting (Git Version Control)

After this setup, every merge to `main` on GitHub reaches the live app by itself:

**GitHub `main` → GitHub Action → cPanel API "pull" → `.cpanel.yml` copies the code into `~/QMS` → pip install → app restart.**

No SSH needed. The deploy copies **only code**. `quotation.db`, `.env`, uploaded files and `utils/*.json` (sessions) are never touched.

You need (one time, ~20 minutes): cPanel access and the GitHub repo's Settings page.

---

## Step 1 — SSH key in cPanel (so cPanel can read the private repo)
1. cPanel → **SSH Access** → **Manage SSH Keys** → **Generate a New Key**.
2. **Key Name:** leave the default `id_rsa`. **Key Password: leave empty**, because cPanel cannot use a key with a password for Git.
   - **Key Type: RSA, Key Size: 4096.** GitHub rejects RSA keys under 3072 bits ("Key is too short"), and cPanel's default is 2048.
   - Click **Generate Key** and accept the warning about the empty password.
   - Made a 2048-bit key already? Delete it first: Manage SSH Keys → `id_rsa` → **Delete Key**, under both Public Keys and Private Keys. Then generate again.
3. Back on Manage SSH Keys, under **Public Keys**, click `id_rsa.pub` → **View/Download**, and copy the whole key (`ssh-rsa AAAA...`).

> If `id_rsa` already exists and is used for something else, ask me — it then needs a `~/.ssh/config` file.

## Step 2 — Add that key to GitHub as a Deploy key
1. GitHub → repo **APQMS-** → **Settings** → **Deploy keys** → **Add deploy key**.
2. Title: `cPanel MilesWeb`. Key: paste. **Leave "Allow write access" unticked.** Click **Add key**.

## Step 3 — Clone the repo in cPanel
1. cPanel → **Git™ Version Control** → **Create**.
2. **Clone a Repository:** on.
3. **Clone URL:** `git@github.com:manavbudhia-dev/APQMS-.git`
4. **Repository Path:** `repositories/APQMS-` (**not** `QMS` — the live folder stays separate).
5. **Repository Name:** `APQMS`. Click **Create**.
6. If cPanel asks to trust GitHub's SSH host key, accept it.

> **"Connection timed out" or port 22 blocked?** Use this Clone URL instead: `ssh://git@ssh.github.com:443/manavbudhia-dev/APQMS-.git`

## Step 4 — First deploy by hand (to check it works)
1. **Backup first:** File Manager → `QMS` → `quotation.db` → Download.
2. Git Version Control → **Manage** (next to APQMS) → **Pull or Deploy** tab.
3. Click **Update from Remote**, then **Deploy HEAD Commit**.
4. **Last Deployment Information** should show success, and the app should still open normally.

> If your Setup Python App "Application root" is not `QMS`, change `~/QMS` and `virtualenv/QMS` in `.cpanel.yml` first.

## Step 5 — cPanel API token (lets GitHub trigger the pull and deploy)
1. cPanel → **Security** → **Manage API Tokens** → **Create**.
2. Name: `github-deploy`. Set no expiry, or set a date and remember to renew it. Click **Create**.
3. **Copy the token now** — cPanel shows it only once.
4. Find your **cPanel username**: cPanel home → right side, **General Information → Current User**.

> This token can do anything your cPanel login can. Keep it only in GitHub Secrets, and keep the repo private.

## Step 6 — Put the details in GitHub Secrets
GitHub → repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add three secrets:

| Name | Value |
|---|---|
| `CPANEL_HOST` | `magnus.herosite.pro` (your cPanel address, without `https://` and `:2083`) |
| `CPANEL_USER` | cPanel username from Step 5 |
| `CPANEL_API_TOKEN` | token from Step 5 |

Optional: if you have more than one Git repo in cPanel, add a **variable** (Variables tab) `CPANEL_REPO_ROOT` = full path shown in Git Version Control, e.g. `/home/USERNAME/repositories/APQMS-`.

## Step 7 — Test it
GitHub → **Actions** → **Deploy to server** → **Run workflow** → `main`.
- **Green tick, with "✅ Deployed …" in the log:** done. Every merge to `main` now deploys by itself.
- **Red:** open the failed step. The first `::error::` line says why:

| Message | Fix |
|---|---|
| `cPanel rejected the login (401/403)` | Wrong `CPANEL_USER` or `CPANEL_API_TOKEN` |
| `Could not reach https://…:2083` | Wrong `CPANEL_HOST`, or the host blocks the API from outside — ask MilesWeb support |
| `No repository in cPanel Git Version Control yet` | Do Step 3 |
| `After pulling, the cPanel clone is at X, not Y` | Git Version Control → Manage → Pull or Deploy → **Update from Remote**, and read the message. Usually the deploy key is missing (Step 2) |
| `Deployment failed. Log on the server: …` | Open that log file in File Manager; send it to me |

## How it works (files)
- `.cpanel.yml` — what cPanel runs on deploy: copy the code folders and files to `~/QMS`, `pip install` into `~/virtualenv/QMS/*/`, then `touch ~/QMS/tmp/restart.txt`.
- `tools/cpanel_deploy.py` — called by the Action. It pulls via cPanel API (`VersionControl/update`), checks the clone reached the pushed commit, starts the deployment (`VersionControlDeployment/create`) and waits for its result.
- `.github/workflows/deploy.yml` — runs the above when the `CPANEL_*` secrets exist. The older SSH deploy still works if `SERVER_*` secrets are set.
