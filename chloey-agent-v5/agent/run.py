"""Chloey Cordova Instagram agent.

Runs on a schedule (GitHub Actions). Each run:
  1. Reads new Telegram messages/button taps from the owner (images, approvals, commands).
  2. Keeps a few posts ready ahead: writes new post ideas when the plan runs out,
     generates the image with Nano Banana Pro (Gemini API), has Claude quality-check it,
     and queues it (auto-approve) or sends it for approval.
  3. Publishes the next approved post if it's a posting day and time.
  4. Reads new Instagram comments and replies / hides / flags them in Chloey's voice.
  5. Saves its state back to the repo.
"""
import base64
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
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
_ALL_REFS = sorted((ROOT / "refs").glob("*.jpg"))
STYLE_REFS = [r for r in _ALL_REFS if "style" in r.name.lower()]
FACE_REFS = [r for r in _ALL_REFS if r not in STYLE_REFS][:10]  # Gemini gets at most 10 faces
ANCHOR = next((r for r in FACE_REFS if r.name.startswith("1-")), FACE_REFS[0] if FACE_REFS else None)
REFS = FACE_REFS + STYLE_REFS  # faces first (anchor first), style reference last

PREFIX = ("Use the woman in reference images 1–5 as the exact same person: same face shape, light blue eyes, "
          "darker defined brows, full lips, and long wavy dirty-golden blonde hair. Do not change her face or body type. "
          "Match the photographic look of reference image 6: muted film tones, natural grain, unretouched.")
SUFFIX = ("Her skin is completely matte with visible pores — no gloss, no sheen, no retouching. "
          "Realistic everyday proportions, like an ordinary 27-year-old, not a model. Candid, slightly imperfect framing. "
          "Any signs, labels, or text in the scene are out of focus and unreadable. No logos. Vertical 4:5 portrait format.")
SHARP = ("Sharp focus, crisp high detail, shot on a modern full-frame camera. Keep grain minimal. "
         "Her face must be clearly in focus.")


def dynamic_prefix():
    """PREFIX rewritten for however many face references are in refs/."""
    n = len(FACE_REFS)
    text = PREFIX.replace("reference images 1–5", f"reference images 1–{n}")
    if STYLE_REFS:
        text = text.replace("reference image 6", f"the last reference image ({n + 1})")
    return text


def setting(key, default=None):
    """State overrides config (so Telegram commands can change settings)."""
    return STATE.get(key, CONFIG.get(key, default))


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


def b64(data):
    return base64.b64encode(data).decode()


def img_block(data):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(data)}}


# ---------------------------------------------------------------- automatic images

def gemini_image(prompt):
    """Generate one image with Nano Banana Pro using the reference photos. Returns raw bytes."""
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": b64(r.read_bytes())}} for r in REFS]
    parts.append({"text": prompt})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "4:5", "imageSize": "2K"}}}
    models = [m for m in dict.fromkeys([CONFIG.get("gemini_model"), "gemini-3-pro-image",
                                        "gemini-3-pro-image-preview"]) if m]
    last = ""
    for model in models:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
        if r.status_code == 404:
            last = f"model {model} not found"
            continue
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Gemini error: {data['error'].get('message', data['error'])}")
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        reason = (data.get("promptFeedback", {}).get("blockReason")
                  or next((c.get("finishReason") for c in data.get("candidates", [])), "no image returned"))
        raise RuntimeError(f"Gemini returned no image ({reason})")
    raise RuntimeError(f"Gemini: {last}")


QC_RULES = """You are the photo editor for an Instagram account about Chloey, an AI-generated travel creator.
You'll see reference photos of Chloey, then one candidate post (the LAST image).
Her face identity is already verified by face-recognition software, so do NOT judge whether it's the
same person. Check only the image quality problems below.
Fail the candidate if ANY of these are true:
- Skin looks glossy, plastic, or heavily retouched
- Hands or fingers are malformed, or there are extra or missing limbs
- There is prominent garbled or misspelled text that a viewer would notice, or a visible brand logo
  (small, blurry, or background signage is fine)
- Her body proportions look exaggerated or unrealistic
- The image is blurry, soft, or her face is out of focus
- Nudity or anything Instagram would remove
Small imperfections are fine; real phone photos aren't perfect.
Answer with JSON only: {"pass": true|false, "issues": "<short list, empty if pass>"}"""


def quality_check(candidate_bytes):
    """Returns (verdict, issues). verdict is True (pass), False (fail), or None (check didn't answer)."""
    content = []
    for i, ref in enumerate(FACE_REFS[:8], 1):
        content += [{"type": "text", "text": f"Reference {i}:"}, img_block(ref.read_bytes())]
    content += [{"type": "text", "text": "CANDIDATE:"}, img_block(candidate_bytes),
                {"type": "text", "text": 'Judge the CANDIDATE. Reply with ONLY the JSON object, e.g. '
                                         '{"pass": true, "issues": ""} — no other words.'}]
    text, stop = "", ""
    for model in dict.fromkeys([CONFIG["claude_model"], CONFIG.get("comment_model")]):
        if not model:
            continue
        msg = claude.messages.create(model=model, max_tokens=1500, system=QC_RULES,
                                     messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        stop = getattr(msg, "stop_reason", "")
        if text:
            break
    m = re.search(r"\{.*\}", text, re.S)
    try:
        result = json.loads(m.group()) if m else None
    except json.JSONDecodeError:
        result = None
    if result is None:
        if re.search(r'"?pass"?\s*[:=]\s*true', text.lower()):
            return True, ""
        return None, f"check gave no verdict ({stop or 'empty'}): {text[:150]}"
    return bool(result.get("pass")), result.get("issues", "")


NEW_POST_RULES = CHARACTER + """

## Your job right now
Plan Chloey's next Instagram post. Answer with JSON only:
{"region": "<place, e.g. LAS VEGAS>", "title": "<short scene name>",
 "category": "Travel" | "Everyday" | "Real life", "swim": true|false,
 "caption": "<caption in her voice, no hashtags>", "hashtags": "<10 hashtags>",
 "scene": "<1-3 sentences describing the photo: where she is, what she's wearing, what she's doing, the light>"}
Rules: keep roughly 60% travel, 25% everyday, 15% unglamorous real life. Don't repeat a recent scene.
Places she goes: Las Vegas, San Diego, Washington DC, Seattle, Florida, Puerto Rico, Italy, US road trips, boats, flights.
Swimwear only when the place calls for it, always tasteful (one-pieces or cover-ups, wide shots). No brands or logos."""


def plan_new_post():
    recent = [f"{p['title']} ({p.get('category', '')})" for p in POSTS[-15:]]
    plan = json.loads(re.search(r"\{.*\}", ask_claude(
        NEW_POST_RULES, "Recent posts, newest last:\n" + "\n".join(recent), max_tokens=700), re.S).group())
    n = max(p["n"] for p in POSTS) + 1
    post = {"n": n, "region": plan["region"].upper(), "title": plan["title"],
            "category": plan.get("category", "Travel"), "swim": bool(plan.get("swim")),
            "caption": plan["caption"], "hashtags": plan["hashtags"],
            "prompt": f"{PREFIX}\n\nScene: {plan['scene']}\n\n{SUFFIX}", "status": "needs_image",
            "planned_by_agent": True}
    POSTS.append(post)
    return post


# ---------------------------------------------------------------- face identity (face recognition)

MODEL_DIR = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "face-models"
MODEL_URLS = {
    "det": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "rec": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
_FACE = {}


def _face_models():
    if not _FACE:
        import cv2
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        paths = {}
        for key, url in MODEL_URLS.items():
            p = MODEL_DIR / url.rsplit("/", 1)[1]
            if not p.exists() or p.stat().st_size < 100_000:
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                p.write_bytes(r.content)
            paths[key] = str(p)
        _FACE["cv2"] = cv2
        _FACE["det"] = cv2.FaceDetectorYN.create(paths["det"], "", (320, 320), 0.8)
        _FACE["rec"] = cv2.FaceRecognizerSF.create(paths["rec"], "")
    return _FACE


def _embedding(data):
    m = _face_models()
    cv2 = m["cv2"]
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    s = min(1.0, 1600 / max(h, w))
    img = cv2.resize(img, (int(w * s), int(h * s)))
    m["det"].setInputSize((img.shape[1], img.shape[0]))
    _, faces = m["det"].detect(img)
    if faces is None:
        return None
    face = max(faces, key=lambda f: f[2] * f[3])  # biggest face in the photo
    return m["rec"].feature(m["rec"].alignCrop(img, face))


def face_score(data):
    """0-100 match against the anchor face (refs/1-*). None if no face is visible."""
    if "anchor" not in _FACE:
        _FACE["anchor"] = _embedding(ANCHOR.read_bytes())
    e = _embedding(data)
    if e is None or _FACE["anchor"] is None:
        return None
    m = _face_models()
    return round(float(m["rec"].match(_FACE["anchor"], e, m["cv2"].FaceRecognizerSF_FR_COSINE)) * 100)


# ---------------------------------------------------------------- character sheet (/refs)

SHEET_SHOTS = [
    "a straight-on head-and-shoulders portrait, neutral expression, plain light gray wall behind her, soft window light",
    "a head-and-shoulders portrait turned three-quarters to her left, soft smile, outdoors in open shade",
    "a head-and-shoulders portrait turned three-quarters to her right, relaxed expression, indoors by a window",
    "a close-up portrait laughing naturally, golden-hour sunlight, outdoors",
    "a close-up portrait, hair pulled back in a loose ponytail, no makeup, overcast daylight",
    "a waist-up photo in a plain white t-shirt, looking at the camera, bright even daylight",
    "a waist-up photo in a navy sweater, slight smile, warm indoor lamp light",
    "a close-up portrait looking slightly up past the camera, soft diffused light, city street blurred behind",
    "a head-and-shoulders portrait in a denim jacket, wind in her hair, cloudy beach behind",
    "a close-up selfie-style portrait at arm's length, natural expression, car interior daylight",
]


def candidate_path(n):
    return ROOT / "refs_candidates" / f"cand-{n:02d}.jpg"


def maybe_build_refs():
    """Generate character-sheet candidates for /refs. Runs even while posting is paused."""
    todo = int(STATE.get("refs_todo", 0))
    if todo <= 0 or not GEMINI_KEY:
        return
    tries = int(STATE.get("refs_tries", 0))
    minimum = int(setting("ref_match_min", 65))
    for _ in range(int(setting("refs_per_run", 3))):
        if todo <= 0:
            break
        if tries >= int(setting("refs_max_tries", 24)):
            say("I've hit the try limit for /refs. Send /refs again to make more.")
            todo = 0
            break
        shot = SHEET_SHOTS[tries % len(SHEET_SHOTS)]
        tries += 1
        prompt = (f"{dynamic_prefix()}\n\nScene: {shot}. Her face must be fully visible and in sharp focus.\n\n"
                  f"{SUFFIX}\n\n{SHARP}")
        raw = gemini_image(prompt)
        n = int(STATE.get("refs_next_id", 1))
        STATE["refs_next_id"] = n + 1
        candidate_path(n).parent.mkdir(exist_ok=True)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        score = face_score(buf.getvalue())
        if score is None or score < minimum:
            continue  # not her: throw away silently
        candidate_path(n).write_bytes(buf.getvalue())
        todo -= 1
        tg("sendPhoto", files={"photo": ("ref.jpg", buf.getvalue(), "image/jpeg")}, chat_id=CHAT_ID,
           caption=f"Reference candidate #{n} — face match {score}/100. Keep it in Chloey's reference set?",
           reply_markup={"inline_keyboard": [[{"text": "✅ Keep", "callback_data": f"refkeep:{n}"},
                                              {"text": "🗑 Toss", "callback_data": f"reftoss:{n}"}]]})
    STATE["refs_todo"] = todo
    STATE["refs_tries"] = tries if todo > 0 else 0
    if todo <= 0 and STATE.get("refs_announce_done"):
        say("That's the batch. Keep or toss the ones above, then send /resume when you're happy with her face.")
        STATE["refs_announce_done"] = False


def maybe_generate():
    """Keep `queue_target` posts ready ahead. Generates at most one image per run."""
    if STATE["paused"] or not GEMINI_KEY or len(REFS) < 2:
        return
    ready = sum(1 for p in POSTS if p["status"] in ("approved", "pending_approval"))
    if ready >= int(setting("queue_target", 3)):
        return
    post = next((p for p in POSTS if p["status"] == "needs_image"), None) or plan_new_post()
    prompt = f"{post['prompt'].replace(PREFIX, dynamic_prefix())}\n\n{SHARP}"
    issues = ""
    for attempt in range(int(setting("max_image_attempts", 2))):
        raw = gemini_image(prompt)
        save_instagram_jpeg(raw, post["n"])
        score = face_score(image_path(post["n"]).read_bytes())
        post["face_score"] = score
        if score is not None and score < int(setting("face_match_min", 60)):
            ok, issues = False, f"face match only {score}/100 — not Chloey"
            continue
        ok, issues = quality_check(image_path(post["n"]).read_bytes())
        if ok is None:
            # The checker didn't answer: don't throw the image away, let the owner decide.
            post.pop("final_caption", None)
            post["status"] = "pending_approval"
            say(f"I couldn't auto-check post {post['n']}, so it needs your OK.")
            send_preview(post, auto=False)
            return
        if ok:
            break
    else:
        post["gen_failures"] = post.get("gen_failures", 0) + 1
        image_path(post["n"]).unlink(missing_ok=True)
        if post["gen_failures"] >= 3:
            post["status"] = "skipped"
            say(f"Skipping post {post['n']} ({esc(post['title'])}) — couldn't get a clean image. Last issue: {esc(issues)}")
        else:
            say(f"Post {post['n']} image didn't pass my check ({esc(issues)}). I'll try again next run.")
        return
    post.pop("final_caption", None)
    if setting("auto_approve", True):
        post["status"] = "approved"
        post["approved_at"] = datetime.now(timezone.utc).isoformat()
        send_preview(post, auto=True)
    else:
        post["status"] = "pending_approval"
        send_preview(post)


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


def send_preview(post, auto=None):
    auto = post["status"] == "approved" if auto is None else auto
    row = [] if auto else [{"text": "✅ Approve", "callback_data": f"approve:{post['n']}"}]
    row += [{"text": "✏️ New caption", "callback_data": f"recap:{post['n']}"},
            {"text": "⏭ Redo image", "callback_data": f"skip:{post['n']}"}]
    rows = [row]
    if auto:
        rows.append([{"text": "🛑 Remove", "callback_data": f"remove:{post['n']}"}])
    header = (f"Queued automatically: post {post['n']} — {post['title']}\n"
              f"Tap a button before it posts if you want a change.\n\n" if auto
              else f"Post {post['n']} — {post['title']}\n\n")
    if post.get("face_score") is not None:
        header = f"Face match: {post['face_score']}/100\n" + header
    with open(image_path(post["n"]), "rb") as f:
        tg("sendPhoto", files={"photo": f}, chat_id=CHAT_ID,
           caption=(header + full_caption(post))[:1020],
           reply_markup={"inline_keyboard": rows})


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
    "/resume — start again\n"
    "/auto on — I queue my own images without waiting for you (you can still veto)\n"
    "/auto off — every image waits for your Approve\n"
    "/refs — make 8 new reference photos of Chloey's face for you to keep or toss\n\n"
    "I make the images myself when the Gemini key is set. You can still add your own: send it "
    "<b>as a file</b> with the post number as the caption (e.g. <b>7</b>). I check in about every 30 minutes."
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
            f"Images: {'automatic' if GEMINI_KEY else 'manual'} · "
            f"Auto-approve: {'on' if setting('auto_approve', True) else 'off'}\n\n"
            + "\n".join(f"{k}: {v}" for k, v in counts.items())
            + f"\n\nQueue (posts in this order): {', '.join(queued) or 'empty'}")
    elif t == "/pause":
        STATE["paused"] = True
        say("Paused. No posting or comment replies until /resume.")
    elif t == "/resume":
        STATE["paused"] = False
        say("Back on.")
    elif t.startswith("/refs"):
        m = re.search(r"\d+", t)
        STATE["refs_todo"] = min(int(m.group()) if m else 8, 12)
        STATE["refs_tries"] = 0
        STATE["refs_announce_done"] = True
        say(f"Building Chloey's character sheet: {STATE['refs_todo']} new reference photos. "
            f"I only send you ones that score {setting('ref_match_min', 65)}+ on face match. "
            f"Tap ✅ Keep or 🗑 Toss on each. A few arrive every check-in.")
    elif t in ("/auto on", "/auto off"):
        STATE["auto_approve"] = t.endswith("on")
        say("Auto-approve is ON. I'll queue images myself; tap Remove or Redo on a preview to stop one."
            if STATE["auto_approve"] else "Auto-approve is OFF. Every image waits for your ✅ Approve.")
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
    if action in ("refkeep", "reftoss") and n.isdigit():
        handle_ref_button(action, int(n))
        return
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
        post.pop("gen_failures", None)
        image_path(post["n"]).unlink(missing_ok=True)
        if GEMINI_KEY:
            say(f"Got it. I'll make a new image for post {post['n']} on my next check.")
        else:
            send_prompt(post)
    elif action == "remove":
        post["status"] = "skipped"
        image_path(post["n"]).unlink(missing_ok=True)
        say(f"Removed post {post['n']} — {esc(post['title'])}. It won't be posted.")


def handle_ref_button(action, n):
    cand = candidate_path(n)
    if not cand.exists():
        say(f"Candidate #{n} is already handled.")
        return
    if action == "refkeep":
        dest = ROOT / "refs" / f"sheet-{n:02d}.jpg"
        dest.write_bytes(cand.read_bytes())
        cand.unlink()
        say(f"Kept #{n}. Chloey now has {len(list((ROOT / 'refs').glob('*.jpg'))) - 1} face references.")
    else:
        cand.unlink()
        say(f"Tossed #{n}.")


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
        if not STATE.get("ig_ok_announced"):
            say(f"✅ Instagram connected as @{esc(_ME.get('username', '?'))}")
            STATE["ig_ok_announced"] = True
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
    errors = STATE.setdefault("errors", {})
    STATE.pop("last_error", None)  # old format
    try:
        fn()
        if errors.pop(name, None):
            say(f"✅ {name} is working again.")
    except Exception as e:
        traceback.print_exc()
        err = str(e)[:400]
        if errors.get(name) != err:  # don't repeat the same alert every run
            say(f"⚠️ {name}: {esc(err)}")
            errors[name] = err


def main():
    if not STATE.get("online_announced"):
        say("Chloey agent is online. 👋\n\n" + HELP)
        STATE["online_announced"] = True
    guarded("Telegram", process_telegram)
    guarded("References", maybe_build_refs)
    guarded("Posting", maybe_publish)
    guarded("Images", maybe_generate)
    guarded("Comments", process_comments)
    STATE_PATH.write_text(json.dumps(STATE, indent=2))
    POSTS_PATH.write_text(json.dumps(POSTS, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
