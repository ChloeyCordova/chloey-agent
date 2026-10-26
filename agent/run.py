"""Chloey Cordova Instagram agent.

Runs on a schedule (GitHub Actions). Each run:
  1. Reads new Telegram messages/button taps from the owner (images, approvals, commands).
  2. Publishes the next approved post if it's a posting day and time.
  3. Reads new Instagram comments and replies / hides / flags them in Chloey's voice.
  4. Saves its state back to the repo.
"""
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "state.json"
POSTS_PATH = ROOT / "posts.json"
STATE = json.loads(STATE_PATH.read_text())
POSTS = json.loads(POSTS_PATH.read_text())
CHARACTER = (ROOT / "character.md").read_text()
TZ = ZoneInfo(CONFIG["timezone"])

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
IG_TOKEN = os.environ["IG_ACCESS_TOKEN"]
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

TG = f"https://api.telegram.org/bot{TG_TOKEN}"
IG = f"https://graph.instagram.com/{CONFIG['instagram_api_version']}"
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


# ---------------------------------------------------------------- helpers

def post_by_n(n):
    return next((p for p in POSTS if p["n"] == n), None)


def image_path(n):
    return ROOT / "images" / f"post-{n:02d}.jpg"


def full_caption(post):
    cap = post.get("final_caption") or post["caption"]
    if CONFIG.get("add_hashtags") and post.get("hashtags"):
        cap = f"{cap}\n\n{post['hashtags']}"
    return cap


def ask_claude(system, user, model=None, max_tokens=600):
    msg = claude.messages.create(
        model=model or CONFIG["claude_model"],
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ---------------------------------------------------------------- Telegram

def tg(method, files=None, **params):
    if files:
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
        r = requests.post(f"{TG}/{method}", data=data, files=files, timeout=60)
    else:
        r = requests.post(f"{TG}/{method}", json=params, timeout=60)
    body = r.json()
    if not body.get("ok"):
        print(f"Telegram {method} failed: {body}", file=sys.stderr)
    return body.get("result")


def say(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text[:4000], parse_mode="HTML",
       disable_web_page_preview=True)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_prompt(post):
    say(f"<b>Post {post['n']} — {esc(post['title'])}</b>"
        f"{' · SWIM (check closely)' if post.get('swim') else ''}\n\n"
        f"In Gemini, attach your 6 reference images, then paste:\n\n"
        f"<pre>{esc(post['prompt'])}</pre>\n\n"
        f"Send me the best image back <b>as a file</b> with the caption <b>{post['n']}</b>.")
    STATE["last_prompt_sent"] = post["n"]


def send_preview(post):
    buttons = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{post['n']}"},
        {"text": "✏️ New caption", "callback_data": f"recap:{post['n']}"},
        {"text": "⏭ Redo image", "callback_data": f"skip:{post['n']}"},
    ]]}
    with open(image_path(post["n"]), "rb") as f:
        tg("sendPhoto", files={"photo": f}, chat_id=CHAT_ID,
           caption=f"Post {post['n']} — {post['title']}\n\n{full_caption(post)}"[:1020],
           reply_markup=buttons)


def download_tg_file(file_id):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def save_instagram_jpeg(raw, n):
    """Convert to JPEG and crop into Instagram's allowed 4:5 .. 1.91:1 range."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    if w / h < 0.8:            # too tall -> crop to 4:5
        new_h = int(w / 0.8)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif w / h > 1.91:         # too wide -> crop to 1.91:1
        new_w = int(h * 1.91)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    if img.width > 1440:
        img = img.resize((1440, int(img.height * 1440 / img.width)))
    image_path(n).parent.mkdir(exist_ok=True)
    img.save(image_path(n), "JPEG", quality=92)


HELP = (
    "<b>Chloey agent commands</b>\n"
    "/next — prompt for the next post that needs an image\n"
    "/prompt 7 — prompt for a specific post\n"
    "/status — where every post stands\n"
    "/pause — stop posting and replying\n"
    "/resume — start again\n\n"
    "To add an image: send it <b>as a file</b> with the post number as the caption (e.g. <b>7</b>). "
    "I'll send a preview with Approve buttons. I check messages about every 30 minutes."
)


def handle_command(text):
    t = text.strip().lower()
    if t in ("/start", "/help"):
        say(HELP)
    elif t == "/next":
        post = next((p for p in POSTS if p["status"] == "needs_image"), None)
        send_prompt(post) if post else say("Every post has an image. Nice.")
    elif t.startswith("/prompt"):
        m = re.search(r"\d+", t)
        post = post_by_n(int(m.group())) if m else None
        send_prompt(post) if post else say("Which post? Example: /prompt 7")
    elif t == "/status":
        counts = {}
        for p in POSTS:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        queued = [str(p["n"]) for p in sorted(
            (p for p in POSTS if p["status"] == "approved"), key=lambda p: p.get("approved_at", ""))]
        say(f"<b>Status</b>{' (PAUSED)' if STATE['paused'] else ''}\n"
            + "\n".join(f"{k}: {v}" for k, v in counts.items())
            + f"\n\nQueue (posts in this order): {', '.join(queued) or 'empty'}")
    elif t == "/pause":
        STATE["paused"] = True
        say("Paused. No posting or comment replies until /resume.")
    elif t == "/resume":
        STATE["paused"] = False
        say("Back on.")
    else:
        say("I didn't catch that. /help shows the commands.")


def handle_image(msg):
    caption = (msg.get("caption") or "").strip()
    m = re.search(r"\d+", caption)
    n = int(m.group()) if m else STATE.get("last_prompt_sent")
    post = post_by_n(n) if n else None
    if not post:
        say("Which post is this for? Send it again with the post number as the caption.")
        return
    if "document" in msg:
        doc = msg["document"]
        if not doc.get("mime_type", "").startswith("image/"):
            say("That file isn't an image.")
            return
        file_id = doc["file_id"]
    else:
        file_id = msg["photo"][-1]["file_id"]  # largest size
        say("Tip: send images as a <b>file</b> instead of a photo so Telegram doesn't shrink them.")
    save_instagram_jpeg(download_tg_file(file_id), post["n"])
    post["status"] = "pending_approval"
    post.pop("final_caption", None)
    send_preview(post)


def handle_button(cb):
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    action, _, n = cb.get("data", "").partition(":")
    post = post_by_n(int(n)) if n.isdigit() else None
    if not post:
        return
    if action == "approve":
        if not image_path(post["n"]).exists():
            say(f"Post {post['n']} has no image yet.")
            return
        post["status"] = "approved"
        post["approved_at"] = datetime.now(timezone.utc).isoformat()
        say(f"Post {post['n']} approved and queued. Posting days: {', '.join(CONFIG['post_days'])} "
            f"after {CONFIG['post_hour']}:00.")
    elif action == "recap":
        post["final_caption"] = ask_claude(
            CHARACTER,
            f"Write a new Instagram caption for this post, in Chloey's voice. "
            f"Scene: {post['title']} ({post['region']}). Previous caption: {full_caption(post)}\n\n"
            f"Return only the caption text, no hashtags, no quotes.",
            max_tokens=200)
        send_preview(post)
    elif action == "skip":
        post["status"] = "needs_image"
        post.pop("final_caption", None)
        image_path(post["n"]).unlink(missing_ok=True)
        send_prompt(post)


def process_telegram():
    updates = tg("getUpdates", offset=STATE["telegram_offset"], timeout=0) or []
    for u in updates:
        STATE["telegram_offset"] = u["update_id"] + 1
        try:
            if "callback_query" in u:
                if u["callback_query"]["message"]["chat"]["id"] == CHAT_ID:
                    handle_button(u["callback_query"])
                continue
            msg = u.get("message")
            if not msg or msg["chat"]["id"] != CHAT_ID:
                continue  # ignore anyone who isn't the owner
            if "photo" in msg or "document" in msg:
                handle_image(msg)
            elif msg.get("text"):
                handle_command(msg["text"])
        except Exception as e:
            traceback.print_exc()
            say(f"Something went wrong handling your last message: {esc(str(e))[:300]}")


# ---------------------------------------------------------------- Instagram

def ig_get(path, **params):
    params["access_token"] = IG_TOKEN
    r = requests.get(f"{IG}/{path}", params=params, timeout=60)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"Instagram error on GET {path}: {body['error'].get('message')}")
    return body


def ig_post(path, **params):
    params["access_token"] = IG_TOKEN
    r = requests.post(f"{IG}/{path}", data=params, timeout=60)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"Instagram error on POST {path}: {body['error'].get('message')}")
    return body


_ME = {}


def me():
    if not _ME:
        _ME.update(ig_get("me", fields="user_id,username"))
    return _ME


def maybe_publish():
    if STATE["paused"]:
        return
    now = datetime.now(TZ)
    today = now.date().isoformat()
    if now.strftime("%a") not in CONFIG["post_days"] or now.hour < CONFIG["post_hour"]:
        return
    if STATE.get("last_post_date") == today:
        return
    queue = sorted((p for p in POSTS if p["status"] == "approved"), key=lambda p: p.get("approved_at", ""))
    if not queue:
        if not STATE.get("empty_queue_warned") == today:
            say("It's a posting day but nothing is approved. Send /next to get a prompt.")
            STATE["empty_queue_warned"] = today
        return
    post = queue[0]
    uid = me().get("user_id") or me()["id"]
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/images/post-{post['n']:02d}.jpg"
    container = ig_post(f"{uid}/media", image_url=url, caption=full_caption(post),
                        is_ai_generated="true")["id"]
    for _ in range(12):  # wait up to ~2 minutes for Instagram to process
        status = ig_get(container, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process post {post['n']} (status {status}).")
        time.sleep(10)
    media_id = ig_post(f"{uid}/media_publish", creation_id=container)["id"]
    link = ig_get(media_id, fields="permalink").get("permalink", "")
    post.update(status="posted", media_id=media_id, posted_at=now.isoformat(), permalink=link)
    STATE["last_post_date"] = today
    left = sum(1 for p in POSTS if p["status"] == "approved")
    say(f"📸 Posted #{post['n']} — {esc(post['title'])}\n{link}\n\n{left} more approved in the queue.")


COMMENT_SYSTEM = CHARACTER + """

## Your job right now
You are moderating and replying to comments on Chloey's Instagram posts.
Decide one action for the comment and answer with JSON only, no other text:
{"action": "reply" | "hide" | "flag" | "ignore", "reply": "<text if replying>", "reason": "<few words>"}

- reply: normal friendly comments, questions about the place, compliments. Short, in her voice.
- hide: spam, scams, bots, links, "DM me", crypto, explicit or sexual comments, harassment, hate.
- flag: anything the owner should see personally: brand or collaboration inquiries, press, someone in distress, legal threats, anything you're unsure about. Do not reply to these.
- ignore: emoji-only or one-word comments that don't need an answer (roughly half of them; reply to the rest with something brief).
If someone asks whether she is real, AI, or a bot, reply honestly that she's an AI-generated creator, keeping it light.
"""


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group()) if m else {"action": "flag", "reason": "unparseable"}


def process_comments():
    if STATE["paused"]:
        return
    if not STATE.get("comments_since"):
        # First run: don't reply to old comments, start from now.
        STATE["comments_since"] = datetime.now(timezone.utc).isoformat()
        return
    since = STATE["comments_since"]
    handled = set(STATE["handled_comments"])
    my_name = me().get("username", "").lower()
    replies = 0
    media = ig_get(f"{me().get('user_id') or me()['id']}/media",
                   fields="id,caption,permalink", limit=CONFIG["posts_to_watch_for_comments"]).get("data", [])
    for m in media:
        comments = ig_get(f"{m['id']}/comments", fields="id,text,username,timestamp", limit=50).get("data", [])
        for c in comments:
            ts = c.get("timestamp", "")
            if c["id"] in handled or (ts and _iso(ts) < _iso(since)):
                continue
            if (c.get("username") or "").lower() == my_name:
                handled.add(c["id"])
                continue
            if replies >= CONFIG["max_comment_replies_per_run"]:
                break  # leave the rest for the next run
            decision = parse_json(ask_claude(
                COMMENT_SYSTEM,
                f"Post caption: {m.get('caption', '')[:500]}\n\n"
                f"Comment from @{c.get('username', 'someone')}: {c.get('text', '')}",
                model=CONFIG["comment_model"], max_tokens=300))
            action = decision.get("action")
            if action == "reply" and decision.get("reply"):
                ig_post(f"{c['id']}/replies", message=decision["reply"][:300])
                replies += 1
            elif action == "hide":
                ig_post(c["id"], hide="true")
            elif action == "flag":
                say(f"🚩 <b>Comment for you</b> ({esc(decision.get('reason', ''))})\n"
                    f"@{esc(c.get('username', '?'))}: {esc(c.get('text', ''))}\n{m.get('permalink', '')}")
            handled.add(c["id"])
    STATE["handled_comments"] = list(handled)[-3000:]


def _iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00"))


# ---------------------------------------------------------------- main

def guarded(name, fn):
    try:
        fn()
    except Exception as e:
        traceback.print_exc()
        err = f"{name}: {e}"[:400]
        if STATE.get("last_error") != err:  # don't repeat the same alert every run
            say(f"⚠️ {esc(err)}")
            STATE["last_error"] = err


def main():
    if not STATE.get("online_announced"):
        say("Chloey agent is online. 👋\n\n" + HELP)
        STATE["online_announced"] = True
    guarded("Telegram", process_telegram)
    guarded("Posting", maybe_publish)
    guarded("Comments", process_comments)
    STATE_PATH.write_text(json.dumps(STATE, indent=2))
    POSTS_PATH.write_text(json.dumps(POSTS, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
