# Chloey Agent — Setup Guide

This runs @chloeycordova for you. Once it's set up:

- **You:** make the images in Gemini and send them to a Telegram bot on your phone, then tap **Approve**.
- **The agent:** posts them on schedule with captions and hashtags, labels each post as AI-generated, replies to comments in Chloey's voice, hides spam and creeps, and texts you anything that needs you (brand inquiries, anything weird).

It runs free on GitHub's servers about every 30 minutes. The only running cost is the Claude API, probably around $5–20 a month.

Setup takes about an hour, once. Do the parts in order.

---

## Part 1 — Instagram (5 min)

1. Open Instagram on your phone, logged into **@chloeycordova**.
2. Profile → ☰ menu → **Account type and tools** → **Switch to professional account** → pick **Creator**. Pick any category (for example, "Digital creator").
3. Edit the bio to include the line: **AI-generated creator ✨**

---

## Part 2 — Telegram bot (5 min)

This is how the agent talks to you.

1. Install **Telegram** on your phone and sign up.
2. In Telegram, search **@BotFather** (blue checkmark) → tap **Start**.
3. Send: `/newbot`
4. When it asks for a name, send: `Chloey Agent`
5. When it asks for a username, send something ending in `bot`, like `chloey_agent_jj_bot` (if it's taken, try another).
6. BotFather replies with a long **token** like `7712345678:AAH...`. Copy it into a note. **This is TELEGRAM_BOT_TOKEN.**
7. Tap the link to your new bot (t.me/your_bot_name) and tap **Start**. (It won't answer yet. That's normal.)
8. Search **@userinfobot** → tap **Start**. It replies with your **Id**, a number like `123456789`. **This is TELEGRAM_CHAT_ID.**

---

## Part 3 — Claude API key (5 min)

1. Go to **console.anthropic.com** and sign up (this is separate from your Claude subscription).
2. **Billing** → add a card and buy **$10** of credits. That should last a while.
3. **API Keys** → **Create Key** → name it `chloey` → copy the key (starts with `sk-ant-`). **This is ANTHROPIC_API_KEY.** You only see it once, so save it.

---

## Part 4 — Meta developer app (15–20 min)

This gives the agent permission to post and manage comments on Chloey's account. Meta changes these screens often, so the button names may be slightly different.

1. Go to **developers.facebook.com** and log in with your Facebook account. If asked, register as a developer (just confirm your info).
2. **My Apps** → **Create App**.
3. App name: `Chloey Agent`. Use your email.
4. Use case: pick **"Manage messaging & content on Instagram"**.
5. Business portfolio: choose **"I don't want to connect a business portfolio yet"** → **Create app**.
6. In the app dashboard, open that Instagram use case → **API setup with Instagram login**.
7. Under permissions, make sure these three are added:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
   - `instagram_business_manage_comments`
8. Find **Generate access tokens** → **Add account** → log into **@chloeycordova** → allow everything.
   - **If it says the account must be a tester:** go to **App roles → Roles → Add People → Instagram Tester**, enter `chloeycordova`. Then in the Instagram app: Settings → **Website permissions** → **Apps and websites** → **Tester invites** → **Accept**. Then try "Add account" again.
9. Click **Generate token** next to the account. Copy the long token. **This is IG_ACCESS_TOKEN.**

Leave the app in **Development** mode. That's fine for your own account, so you don't need Meta's app review.

---

## Part 5 — GitHub (15 min)

### 5a. Account and repository
1. Sign up at **github.com** if you don't have an account.
2. Top right **+** → **New repository**.
   - Name: `chloey-agent`
   - Set to **Public**. Instagram needs to download the images from a public link. Your keys and tokens are stored separately as encrypted secrets, and nobody can see them.
   - Don't check any boxes → **Create repository**.

### 5b. Upload the agent files
1. On your PC, right-click **chloey-agent.zip** (in your Claude Downloads folder) → **Extract All** → **Extract**.
2. Open the extracted **chloey-agent** folder. Select **everything inside it** (Ctrl+A). You should see `.github`, `agent`, `images`, `character.md`, `config.json`, `posts.json`, `README.md`, `requirements.txt`, `state.json`.
3. Back on the GitHub page for your new repo, click **"uploading an existing file"**.
4. Drag all the selected files and folders onto the page. Wait for them to finish → **Commit changes**.
5. Check that the repo now shows a `.github` folder. If it doesn't, drag the `.github` folder in again by itself.

### 5c. Let the agent save its progress
1. In the repo: **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions** → select **Read and write permissions** → **Save**.

### 5d. Token that keeps Instagram connected
The Instagram token expires every 60 days. The agent renews it every Monday, but it needs this permission to save the new one.

1. Click your profile picture (top right) → **Settings** → **Developer settings** (bottom left) → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name: `chloey-secrets`. Expiration: the longest allowed (set a calendar reminder to make a new one before it expires).
3. **Repository access** → **Only select repositories** → pick `chloey-agent`.
4. **Permissions** → **Repository permissions** → **Secrets** → **Read and write**.
5. **Generate token** → copy it. **This is GH_PAT.**

### 5e. Add your five secrets
In the repo: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**. Add each one. The name must match exactly:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from Part 3 |
| `IG_ACCESS_TOKEN` | from Part 4 |
| `TELEGRAM_BOT_TOKEN` | from Part 2, step 6 |
| `TELEGRAM_CHAT_ID` | from Part 2, step 8 |
| `GH_PAT` | from Part 5d |

### 5f. Turn it on
1. Click the **Actions** tab. If you see a button about enabling workflows, click it.
2. Click **Chloey agent** on the left → **Run workflow** → **Run workflow**.
3. Within a minute or two, your Telegram bot should message you: **"Chloey agent is online."** 🎉

If it doesn't, click the run in the Actions tab. A red ✗ shows the error. Copy the error text and send it to Claude.

---

## Using it day to day

**Making images (do a week's worth in one sitting):**
1. Open your content plan (chloey-content-plan.docx) or send the bot `/next`.
2. In Gemini, attach the **same 6 reference images in the same order** every time:
   1. Car close-up, black top
   2. Couch, gray sweater
   3. Armchair, gray tank
   4. Laptop, white long-sleeve
   5. Car selfie, navy blazer
   6. Big Sur (style only)
3. Paste the prompt and generate 3–4 versions.
4. Check the image: same face, matte skin, normal proportions, five fingers, no garbled text.
5. Send the best one to your bot **as a file** (📎 → File, not Photo) and type the **post number** as the caption, e.g. `19`.

**Approving:** the bot sends back a preview with three buttons:
- **✅ Approve:** it joins the queue.
- **✏️ New caption:** Claude writes a fresh one in Chloey's voice.
- **⏭ Redo image:** it clears the image and resends the prompt.

**Posting:** one post at about 5pm Pacific on Mon, Tue, Thu, Fri, and Sun, in the order you approved them. You get a message with the link each time.

**Comments:** handled automatically every run. 🚩 messages are the ones it wants you to see.

**Bot commands:**
- `/next`: prompt for the next post that needs an image
- `/prompt 7`: prompt for a specific post
- `/status`: what's done, waiting, and queued
- `/pause` and `/resume`: stop or restart all posting and replying

**Timing:** the agent checks in about every 30 minutes (GitHub sometimes runs late at busy times), so button taps take effect at the next check. To make it act right now: GitHub → Actions → Chloey agent → Run workflow.

---

## Changing settings

Edit **config.json** in GitHub (click the file → ✏️ pencil → change → Commit):
- `post_days`: which days to post
- `post_hour`: hour to post (24-hour clock, Pacific)
- `max_comment_replies_per_run`: cap on replies per check

## Costs
- GitHub, Telegram, and the Instagram API: free.
- Claude API: pay-as-you-go from the credit you added. Check usage at console.anthropic.com.
- Gemini images: covered by your Google AI plan while you make them by hand.

## If something breaks
- **Bot is silent:** Actions tab → open the latest run → look for the red ✗.
- **"Instagram error … token":** generate a new token (Part 4, step 9) and paste it over `IG_ACCESS_TOKEN`.
- **Weekly "Refresh Instagram token" run fails:** your `GH_PAT` probably expired. Make a new one (Part 5d).
- **GitHub disabled the schedule:** this can happen to repos with no activity for 60 days. Actions tab → re-enable.
