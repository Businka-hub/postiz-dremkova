"""Move scheduled (QUEUE) posts from Postiz Cloud to the self-hosted instance.

Usage:
  python -I migrate_queue.py --posts posts_full.json --map channel_map.json \
      --new-url https://postiz.dremkova.com/api --new-key-file new.key [--dry-run]

posts_full.json  export of Cloud posts (public API list + "images" per post)
channel_map.json {"<cloud integration id>": "<new integration id>", ...}
Idempotent: every migrated post is written to migrated.json and skipped next run.
Nothing is deleted on Cloud. To stop Cloud from publishing the originals, switch
them to draft afterwards (reversible): `postiz posts:status <id>` for each id in
migrated.json.
"""
import argparse, datetime as dt, json, os, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--posts", required=True)
ap.add_argument("--map", required=True)
ap.add_argument("--new-url", required=True)
ap.add_argument("--new-key-file", required=True)
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--state", default="migrated.json")
a = ap.parse_args()


def call(base, k, method, path, body=None):
    req = urllib.request.Request(
        base.rstrip("/") + "/public/v1" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": k, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}


new_key = open(a.new_key_file, encoding="utf-8").read().strip()
cmap = json.load(open(a.map, encoding="utf-8"))
state = json.load(open(a.state, encoding="utf-8")) if os.path.exists(a.state) else {}
soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)

posts = [p for p in json.load(open(a.posts, encoding="utf-8"))
         if p["state"] == "QUEUE"
         and dt.datetime.fromisoformat(p["publishDate"].replace("Z", "+00:00")) > soon]
print(f"{len(posts)} future queued posts, {len(state)} already migrated")

for p in sorted(posts, key=lambda x: x["publishDate"]):
    if p["id"] in state:
        continue
    old_int = p["integration"]["id"]
    if old_int not in cmap:
        print("SKIP (channel not mapped):", p["id"], p["integration"]["name"])
        continue
    if a.dry_run:
        print("would migrate", p["publishDate"], p["integration"]["name"], len(p.get("images", [])), "media")
        continue
    media = [call(a.new_url, new_key, "POST", "/upload-from-url", {"url": u}) for u in p.get("images", [])]
    settings = json.loads(p["settings"]) if isinstance(p["settings"], str) else (p["settings"] or {})
    res = call(a.new_url, new_key, "POST", "/posts", {
        "type": "schedule", "date": p["publishDate"], "shortLink": False, "tags": [],
        "posts": [{
            "integration": {"id": cmap[old_int]},
            "value": [{"content": p["content"], "image": [{"id": m["id"], "path": m["path"]} for m in media]}],
            "settings": settings,
        }],
    })
    new_id = (res[0] if isinstance(res, list) else res).get("postId") if res else None
    state[p["id"]] = {"new": new_id, "date": p["publishDate"]}
    json.dump(state, open(a.state, "w", encoding="utf-8"), indent=1)
    print("migrated", p["publishDate"], p["integration"]["name"], "->", new_id)
