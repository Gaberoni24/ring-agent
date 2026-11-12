# ring-agent

Talk to your **Pebble Index 01** ring, and the right thing happens:

- *"Remind me to call the bank tomorrow"* → a task in **TickTick**
- *"Dentist Tuesday at 10am"* → an event in **Google Calendar**
- *"Idea: a podcast about old maps"* → a note in **Notion**
- *"What's the capital of Peru?"* → the answer, pushed to your phone

A few seconds later your phone shows a notification saying exactly what was
done. Tap it to open the item, or tap **Undo**. Anything risky (deleting,
bulk changes) asks first with **Approve / Deny** buttons.

## How it works (the short version)

```
You speak to the ring
  → the Pebble app on your phone turns it into text
  → sends the text to YOUR small server on the internet
  → the server asks Claude (an AI) what you meant
  → Claude adds the task / event / note using your accounts
  → you get a push notification (via the free ntfy app)
```

You run your own copy, with your own accounts and keys. Nothing is shared
with anyone else.

**Plan about 2–3 hours** for the first setup. You don't need to know how to
program; you'll copy and paste commands. Where a step happens on a website,
the clicks are spelled out.

---

## What you need

| What | Why | Cost |
|---|---|---|
| Pebble Index 01 ring + the Pebble phone app | Records your voice and turns it into text | You have it |
| **Anthropic API** account ([console.anthropic.com](https://console.anthropic.com)) | The AI that decides what you meant | Pay as you go, see costs below |
| **A server** — [Oracle Cloud](https://signup.cloud.oracle.com) free tier recommended, or any Linux server | Receives captures 24/7 | $0 on Oracle free tier (a card is needed to sign up) |
| **DuckDNS** ([duckdns.org](https://www.duckdns.org)) | A free web address for your server, e.g. `my-ring.duckdns.org` | Free |
| **ntfy** app on your phone ([ntfy.sh](https://ntfy.sh)) | Push notifications | Free |
| **TickTick** account | Tasks | Free plan works |
| **Notion** account | Notes | Free plan works |
| **Google** account + **Google Cloud** project | Calendar events | Free |
| A computer (Windows, Mac or Linux) | To set things up | — |

You can skip TickTick, Notion or Google at first: anything that isn't set
up falls back to something that is, and the notification tells you so.

### Rough monthly cost

| Item | Monthly |
|---|---|
| Anthropic API | A simple task or event costs **1–3¢**. A Notion note costs **10–15¢** because Claude reads the page first. **10 captures a day ≈ $3–10/month.** |
| Oracle Cloud server | **$0** (Always Free tier). Another provider's small server is about $4–6. |
| DuckDNS, ntfy, Google Calendar API, TickTick/Notion free plans | **$0** |

Tip: in the Anthropic console, set a **monthly spend limit** (Settings →
Limits) so a bug can never surprise you.

---

## Setup

Throughout this guide:

- **`my-ring.duckdns.org`** stands for *your* server's web address. Replace
  it with yours everywhere.
- "**Terminal**" means: on Windows, **Git Bash** (installed in step 0); on
  Mac, the **Terminal** app.
- Lines in grey boxes are commands: copy them, paste into the terminal,
  press Enter.

### Step 0 — Install the tools on your computer

1. **Git**: [git-scm.com/downloads](https://git-scm.com/downloads). On
   Windows, keep all the default options; this also installs **Git Bash**.
2. **Python 3.12 or newer**: [python.org/downloads](https://www.python.org/downloads/).
   On Windows, tick **"Add python.exe to PATH"** on the first installer screen.
3. Get this project. In the terminal:

   ```bash
   cd ~
   git clone <the GitHub URL of this repo> ring-agent
   cd ring-agent
   ```

   (On GitHub: green **Code** button → copy the HTTPS URL.)

4. Set up Python for the project:

   ```bash
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt -e .      # Windows
   # .venv/bin/pip install -r requirements.txt -e .        # Mac/Linux
   ```

   On Mac/Linux, use `.venv/bin/` wherever this guide says `.venv/Scripts/`.
   If `python` isn't found on Mac, use `python3`.

5. Make your own settings file from the template:

   ```bash
   cp .env.example .env
   ```

   Open `.env` in any text editor (Notepad, TextEdit, VS Code). You'll fill
   it in as you go. **Never share or upload this file**: it holds your
   passwords. (It's already listed in `.gitignore`, so git won't upload it.)

### Step 1 — Make an SSH key (your login key for the server)

In the terminal:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ring_server -N ""
```

This creates two files: `~/.ssh/ring_server` (private, stays on your
computer) and `~/.ssh/ring_server.pub` (public, you'll give it to the
server). On Windows, `~` is `C:\Users\<your user name>`.

In `.env`, set:

```
RING_SSH_KEY=~/.ssh/ring_server
```

### Step 2 — Create the server (Oracle Cloud free tier)

> Using a different provider (DigitalOcean, Hetzner, a Raspberry Pi with a
> public IP...)? Create an **Ubuntu 24.04** server, add your
> `ring_server.pub` key, make sure ports **80** and **443** are open, note
> its public IP and SSH user, and skip to step 3.

1. Sign up at **[signup.cloud.oracle.com](https://signup.cloud.oracle.com)**.
   A card is required for identity checks; Always Free resources are not
   charged. Your **Home Region** can never be changed, so pick one near you.
2. In the Oracle console: **☰ menu → Compute → Instances → Create instance**.
   - **Image**: click *Change image* → **Canonical Ubuntu** → **24.04**.
   - **Shape**: click *Change shape* → **Ampere** → **VM.Standard.A1.Flex**,
     1 OCPU / 6 GB memory. If it later says *"Out of capacity"*, choose
     **Specialty and previous generation → VM.Standard.E2.1.Micro** instead
     (also free, and plenty for this app).
   - **Networking**: *Create new virtual cloud network* and *Create new
     public subnet*; make sure **Assign a public IPv4 address** is on.
   - **Add SSH keys**: choose **Upload public key files (.pub)** and pick
     `ring_server.pub` from your `.ssh` folder. (On Windows the `.ssh`
     folder may be hidden: type `%USERPROFILE%\.ssh` in the file dialog's
     address bar.)
   - Click **Create**. Wait until it says **Running**, then copy the
     **Public IP address** from the instance page.
3. **Open the web ports** in Oracle's cloud firewall: on the instance page,
   click the **Subnet** link → **Security** (or *Security Lists*) →
   **Default Security List** → **Add Ingress Rules**:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: **TCP**
   - Destination Port Range: `80,443`
   - Click **Add Ingress Rules**.
4. **Strongly recommended: stop Oracle from shutting it down.** Oracle can
   reclaim free servers that look idle for 7 days, and this one will look
   idle. Fix: **☰ → Billing & Cost Management → Upgrade and Manage
   Payment → Upgrade to Pay As You Go**. Free-tier resources stay free.
   Then add a safety net: **Billing → Budgets → Create Budget**, amount
   `$1`, with an email alert.
5. Check you can log in (replace the IP):

   ```bash
   ssh -i ~/.ssh/ring_server ubuntu@123.45.67.89
   ```

   Type `yes` if asked about the fingerprint. You should see an Ubuntu
   welcome message. Type `exit` to leave.

### Step 3 — Give the server a web address (DuckDNS)

HTTPS certificates need a name, not a bare IP address.

1. Go to **[duckdns.org](https://www.duckdns.org)**, sign in (e.g. with
   Google or GitHub).
2. Type a subdomain (e.g. `my-ring`) → **add domain**. In the **current
   ip** box next to it, paste your server's public IP → **update ip**.
3. Wait a minute, then check in the terminal: `ping my-ring.duckdns.org`
   should show your server's IP (press Ctrl+C to stop).
4. In `.env`, set (with your own name):

   ```
   PUBLIC_BASE_URL=https://my-ring.duckdns.org
   RING_DOMAIN=my-ring.duckdns.org
   RING_HOST=ubuntu@my-ring.duckdns.org
   ```

### Step 4 — Make your two private passwords

Run this **twice** in the terminal; each run prints a new random string:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the first one in `.env` as `WEBHOOK_SECRET=` (the password the ring app
uses to talk to your server) and the second as `NTFY_TOPIC=` (your private
notification channel name: anyone who knows it could read your
notifications).

Also set your timezone, using a name from
[this list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
(column "TZ identifier"):

```
RING_TZ=America/Chicago
```

### Step 5 — Anthropic API key (required)

1. Go to **[console.anthropic.com](https://console.anthropic.com)** and
   sign up. This is separate from a Claude.ai subscription; it's
   pay-as-you-go.
2. **Settings → Billing**: add a card and buy **$5–10 of credit**.
3. **Settings → Limits**: set a monthly limit you're comfortable with.
4. **API Keys → Create Key**, name it `ring-agent`, copy it. Put it in
   `.env`:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

### Step 6 — TickTick (tasks)

1. Sign in at **[ticktick.com](https://ticktick.com)** on the web.
2. Click your **avatar** (top left) → **Settings** → **Account** →
   **API Token** → create/copy the token.
3. In `.env`: `TICKTICK_API_TOKEN=<the token>`

### Step 7 — Notion (notes)

1. In Notion, create two pages if you don't have them: **Inbox** (for
   captures the agent isn't sure about) and **Ideas**. Optionally a third
   page, **Undone**, where the Undo button logs what it reversed.
2. Go to **[notion.so/my-integrations](https://www.notion.so/my-integrations)**
   → **New integration** → name `ring-agent`, type **Internal**, pick your
   workspace → **Save**. Copy the **Internal Integration Secret** into
   `.env` as `NOTION_TOKEN=`.
3. **Important — the #1 reason Notion "doesn't work":** the integration
   can't see anything until you share pages with it. For each page the
   agent should use (Inbox, Ideas, Undone, and any others): open the page
   → **⋯** (top right) → **Connections** → find `ring-agent` → **Confirm**.
   Sharing a page also shares everything inside it, so the simplest option
   is to put these pages under one parent page and share that.
4. Optional: open the **Undone** page, copy its link (⋯ → Copy link). The
   32-character code at the end of the link (before any `?`) is the page
   ID. Put it in `.env` as `NOTION_UNDO_PAGE_ID=`.

You'll also connect Notion's newer "MCP" login after deploying (step 12).
It gives the agent smarter search; the token above is the fallback.

### Step 8 — Google Calendar (events)

This is the fiddliest step. Take it slowly; you only do it once.

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)**,
   sign in with the Google account whose calendar you want.
2. Top bar → project picker → **New Project** → name `ring-agent` →
   **Create**. Make sure the new project is selected in the top bar.
3. **☰ → APIs & Services → Library** → search **Google Calendar API** →
   **Enable**.
4. **☰ → APIs & Services → OAuth consent screen** (may be called **Google
   Auth Platform**) → **Get started**:
   - App name `ring-agent`, your email as support email → **Next**.
   - Audience: **External** → **Next**. Contact email: yours → **Next** →
     agree → **Create**.
5. **Audience** (left menu) → under *Test users* add your own Gmail
   address. Then, at the top, click **Publish app** → **Confirm**, so the
   status says **In production**.
   **Don't skip publishing.** In "Testing" mode Google logs the app out
   every 7 days and Calendar silently stops working.
6. **Clients** (left menu) → **Create client** → Application type
   **Desktop app** → name `ring-agent` → **Create** → **Download JSON**.
7. Rename the downloaded file to exactly **`google_client_secret.json`**
   and move it into the project folder (next to `.env`).
8. In the terminal, from the project folder:

   ```bash
   .venv/Scripts/python scripts/setup_google.py
   ```

   A browser opens. Pick your Google account. You'll see **"Google hasn't
   verified this app"**; that's expected for a personal app: click
   **Advanced** → **Go to ring-agent (unsafe)** → **Continue**. When the
   browser says the flow is complete, a new file `google_token.json`
   appears in the project folder. Deploying (step 11) uploads it.

To use a calendar other than your main one: Google Calendar → that
calendar's **Settings** → *Integrate calendar* → copy the **Calendar ID**
into `.env` as `GOOGLE_CALENDAR_ID=`.

### Step 9 — Tell the agent your own rules

```bash
cp context/CONTEXT.example.md context/CONTEXT.md
```

Open `context/CONTEXT.md` and replace every `<PLACEHOLDER>` with your own
details: your TickTick list names and what goes in each, your Notion pages,
how you like dates handled. Write it like instructions to an assistant;
plain English is fine. A few minutes here is what makes "add it to my
work list" land in the right place.

`context/CONTEXT.md` stays on your computer and your server; it's in
`.gitignore`, so it never gets uploaded to GitHub. You can also teach the
agent later just by saying *"from now on, put recipes in my Cooking page"*
to the ring.

### Step 10 — Prepare the server (one time)

```bash
scp -i ~/.ssh/ring_server scripts/provision.sh ubuntu@my-ring.duckdns.org:
ssh -i ~/.ssh/ring_server ubuntu@my-ring.duckdns.org sudo bash provision.sh
```

This takes a few minutes. It installs Python and Caddy (which handles
HTTPS automatically), opens the firewall for web traffic, creates the app
folder `/opt/ring-agent`, and sets up nightly backups and automatic
security updates. Safe to run again if something interrupts it.

The server reboots itself at 08:30 UTC when a security update needs it
(about 90 seconds offline). To pick a different time, edit
`Automatic-Reboot-Time` in `scripts/provision.sh` before running it.

### Step 11 — Deploy (send the app to the server)

```bash
PUSH_SECRETS=1 bash scripts/deploy.sh
```

This copies the app, uploads `.env` and `google_token.json` securely,
sets up HTTPS for your address, installs everything, starts the service,
and ends with **`deploy OK`**.

Check it from your browser: open `https://my-ring.duckdns.org/health`. You
should see `{"ok":true,"queue_depth":0}` with no security warning.

Then build the agent's map of your Notion pages:

```bash
ssh -i ~/.ssh/ring_server ubuntu@my-ring.duckdns.org \
  "cd /opt/ring-agent && sudo -u ring .venv/bin/python scripts/refresh_notion_map.py"
```

If it reports 0 items, go back to step 7.3 (sharing pages).

**Later changes:** after editing `context/CONTEXT.md` or the code, run
`bash scripts/deploy.sh`. Add `PUSH_SECRETS=1` in front whenever you
changed `.env` or redid the Google step.

### Step 12 — Connect Notion's smart search (one time)

```bash
ssh -i ~/.ssh/ring_server ubuntu@my-ring.duckdns.org \
  "cd /opt/ring-agent && sudo -u ring .venv/bin/python scripts/notion_oauth.py begin"
```

It prints a link. Open it (your phone is fine), choose your workspace, and
approve. You'll land on a page saying **"Notion connected to ring-agent"**.
If this login ever expires, the agent quietly falls back to the
`NOTION_TOKEN` from step 7 and the daily check sends you a notification to
run `begin` again.

### Step 13 — Phone: notifications and the ring

1. Install the **ntfy** app (Android / iPhone). Tap **+** → *Subscribe to
   topic* → paste your `NTFY_TOPIC` value exactly → **Subscribe**.
   On Android, allow notifications and turn off battery optimisation for
   ntfy, or they may arrive late.
2. In the **Pebble app** → your Index ring's settings → **Webhook**:
   - **URL**: `https://my-ring.duckdns.org/webhook`
   - **Header**: name `Authorization`, value `Bearer ` followed by your
     `WEBHOOK_SECRET` (the word Bearer, one space, then the secret; no
     quotes)
   - **Payload**: **Both** (the text drives the agent; the audio is kept
     on your server for checking mis-heard captures)
   - **Trigger**: e.g. *Double click and hold*

   The Pebble app changes often. If the screens don't match, look for
   `INDEX_WEBHOOK_API.md` in the
   [coredevices/mobileapp](https://github.com/coredevices/mobileapp) repo.

### Step 14 — Test it

**First, without the ring**, from the project folder in the terminal:

```bash
SECRET=$(sed -n 's/^WEBHOOK_SECRET=//p' .env)
curl -i -X POST https://my-ring.duckdns.org/webhook \
  -H "Authorization: Bearer $SECRET" \
  -F "transcription=add buy oat milk to ticktick for tomorrow" \
  -F "recordedAt=$(date +%s000)" \
  -F "client=ring"
```

You should get `HTTP/2 200` within a second, then within ~15 seconds a
TickTick task and an ntfy notification. Also try
`transcription=dentist appointment Tuesday at 10am` (→ Calendar) and
`transcription=idea: a podcast about old maps` (→ Notion).

These create **real** items: delete them afterwards.

See what the agent did with each capture (what it heard, what it decided,
every step it took):

```bash
bash scripts/captures.sh        # last 5 captures
bash scripts/captures.sh 20     # last 20
```

**Then with the ring:** record "remind me to water the plants tomorrow"
and wait for the notification.

### Step 15 — Get told if the server goes down (recommended)

The phone does **not** retry: if your server is down, that capture is lost.
Create a free monitor at **[uptimerobot.com](https://uptimerobot.com)**:
*New monitor* → type **HTTP(s)** → URL `https://my-ring.duckdns.org/health`
→ interval 5 minutes → email alert. A daily check on the server also
notifies you about any capture that got stuck.

---

## Troubleshooting

### The webhook / ring

| Symptom | Fix |
|---|---|
| `https://my-ring.duckdns.org/health` doesn't load at all | 1) `ping my-ring.duckdns.org` shows the right IP? If not, fix it on duckdns.org. 2) Oracle ports 80/443 open (step 2.3)? 3) Server running? Check in the Oracle console: if it's *Stopped*, start it and do the Pay As You Go upgrade (step 2.4). |
| Browser shows a certificate/security warning | Caddy couldn't get a certificate yet, usually because DNS or ports were wrong when it tried. Fix those, then `bash scripts/deploy.sh` again. Details: `ssh -i ~/.ssh/ring_server ubuntu@my-ring.duckdns.org "sudo journalctl -u caddy -n 50"` |
| Test command returns **401 unauthorized** | The secret doesn't match. The header must be exactly `Bearer <secret>`: capital B, one space, no quotes, no extra spaces. If you changed `WEBHOOK_SECRET` in `.env`, redeploy with `PUSH_SECRETS=1`. |
| Returns **200**, but nothing appears and no notification | Run `bash scripts/captures.sh`. If the status is `failed`, the outcome line says why. Common causes: Anthropic credit ran out ("credit balance is too low": top up at console.anthropic.com), or a wrong API key. |
| Item is created but no notification | Your ntfy subscription must match `NTFY_TOPIC` exactly (it's case-sensitive). On Android, turn off battery optimisation for ntfy. |
| Ring captures never arrive, but the test command works | The Pebble app's webhook URL must be `https://` and end in `/webhook`; check the header format above. Make sure the trigger gesture you use is the one set for the webhook. |
| Notification buttons (Undo / Approve) fail when tapped | `PUBLIC_BASE_URL` in `.env` must be exactly `https://my-ring.duckdns.org` (no trailing slash); redeploy with `PUSH_SECRETS=1`. Buttons expire after a few days. |
| Server logs | `ssh -i ~/.ssh/ring_server ubuntu@my-ring.duckdns.org "sudo journalctl -u ring-agent -n 100"` |

### Google Calendar / Google login

| Symptom | Fix |
|---|---|
| `google_client_secret.json not found` | The downloaded file must be renamed exactly to `google_client_secret.json` and sit in the project folder next to `.env`. Windows may hide the `.json` extension; make sure it isn't `google_client_secret.json.json`. |
| **"Access blocked: ring-agent has not completed the Google verification process"** / *Error 403: access_denied* | Add your Gmail address under **Audience → Test users**, and make sure the app is **published** (step 8.5). |
| **"Google hasn't verified this app"** | Normal for a personal app: **Advanced → Go to ring-agent (unsafe)**. |
| *Error 400: redirect_uri_mismatch* | You created a **Web application** client. Create a new **Desktop app** client (step 8.6) and download that JSON instead. |
| Calendar worked for about a week, then stopped (`invalid_grant`, "token expired or revoked") | The app was still in **Testing**. Publish it (step 8.5), then rerun `scripts/setup_google.py` and `PUSH_SECRETS=1 bash scripts/deploy.sh`. The same fix applies if you changed your Google password or removed the app's access. |
| `Google Calendar API has not been used in project ... or it is disabled` | Enable it (step 8.3) in the **same** project as your OAuth client, wait a few minutes. |
| Events land in TickTick instead of Calendar | Calendar isn't set up on the server: `google_token.json` wasn't uploaded. Redeploy with `PUSH_SECRETS=1`. |

### Other

| Symptom | Fix |
|---|---|
| Notes go to Inbox instead of the right page, or "Notion is not configured" | Share the pages with your integration (step 7.3), then rerun the Notion map command in step 11. |
| TickTick: "no TickTick list named ..." | The list names in `context/CONTEXT.md` must match your real list names (emoji and capitals don't matter). Redeploy after editing. |
| `deploy.sh` says `set RING_HOST=user@host` | Fill in `RING_HOST` in `.env` (step 3.4). |
| `ssh: Permission denied (publickey)` | `RING_SSH_KEY` must point at the private key (`~/.ssh/ring_server`, not the `.pub`), and that key must be the one you gave Oracle. |
| Times are off by some hours | Set `RING_TZ` in `.env` (step 4), redeploy with `PUSH_SECRETS=1`. |

---

## Everyday use

- **Change where things go:** edit `context/CONTEXT.md`, then
  `bash scripts/deploy.sh`. Or just tell the ring: *"from now on, put
  recipes in Cooking"*. It remembers rules you speak in
  `context/LEARNED.md` on the server.
- **Undo:** tap **Undo** on the notification, or say *"undo that"*.
- **Corrections:** *"actually, make that Friday"* fixes the last capture.
- **Think harder:** say *"think hard about..."* to use a bigger Claude model.
- **See what happened:** `bash scripts/captures.sh`.

## Privacy and security

- Your server stores every transcript (and the audio) in
  `/opt/ring-agent/captures.db` and `audio/`, with nightly backups kept for
  14 days. Only you can log in to it.
- Transcripts are sent to Anthropic's API so Claude can act on them, and
  to TickTick / Google / Notion when items are created.
- Keep `.env`, `google_client_secret.json`, `google_token.json` and
  `context/CONTEXT.md` private. They're all in `.gitignore`. If one ever
  leaks, create a new key at the matching service and redeploy with
  `PUSH_SECRETS=1`.

## For tinkerers

- Run the tests: `pip install -r requirements-dev.txt` then
  `.venv/Scripts/python -m pytest -q`.
- Run locally: fill in `.env`, then
  `.venv/Scripts/uvicorn app.main:app --port 8000`.
- `CLAUDE.md` explains the code layout and the lessons learned. It's
  written for AI coding assistants like Claude Code, but it's readable by
  humans too. New integrations are one file each in `app/tools/`.
- The webhook format comes from `INDEX_WEBHOOK_API.md` in
  [coredevices/mobileapp](https://github.com/coredevices/mobileapp).

## License

MIT — see [LICENSE](LICENSE).
