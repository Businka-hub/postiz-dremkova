"""Smoke test of the self-hosted Postiz: web, public API, MCP, worker, channels, backups.

Usage:
  python -I smoke_test.py --url https://postiz.dremkova.com --env ~/.claude/.env [--ssh businka-prod]

--env   file with POSTIZ_SELF_API_KEY=... (the key is never printed)
--ssh   ssh host of the server; adds container, worker, channel, post and backup checks
Prints PASS / WARN / FAIL per check, exit code 1 when anything FAILs.
Creates one draft post and deletes it again; nothing is published.
"""
import argparse, datetime as dt, json, os, socket, ssl, subprocess, urllib.error, urllib.request
from urllib.parse import urlparse

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="https://postiz.dremkova.com")
ap.add_argument("--env", default=os.path.expanduser("~/.claude/.env"))
ap.add_argument("--ssh")
a = ap.parse_args()

key = next((l.split("=", 1)[1].strip() for l in open(a.env, encoding="utf-8")
            if l.startswith("POSTIZ_SELF_API_KEY=")), "")
results = []


def check(name, fn):
    try:
        status, info = fn()
    except Exception as e:  # a crashed check is a failed check
        status, info = "FAIL", f"{type(e).__name__}: {e}"[:200]
    results.append(status)
    print(f"{status:4}  {name:28} {info}")


def http(method, path, body=None, headers=None, auth=True):
    h = {"Content-Type": "application/json", **(headers or {})}
    if auth:
        h.setdefault("Authorization", key)
    req = urllib.request.Request(a.url + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def remote(cmd):
    return subprocess.run(["ssh", a.ssh, cmd], capture_output=True, text=True, timeout=120).stdout.strip()


def sql(q):
    return remote(f"docker exec postiz-postgres psql -U postiz -d postiz -tAF'|' -c {json.dumps(q)}")


def tls():
    host = urlparse(a.url).hostname
    with socket.create_connection((host, 443), timeout=15) as s:
        with ssl.create_default_context().wrap_socket(s, server_hostname=host) as t:
            end = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(t.getpeercert()["notAfter"]))
    days = (end - dt.datetime.now()).days
    return ("PASS" if days >= 14 else "FAIL"), f"certificate valid {days} more days"


def web():
    code, _ = http("GET", "/", auth=False)
    return ("PASS" if code in (200, 307) else "FAIL"), f"HTTP {code}"


def registration():
    code, t = http("GET", "/api/auth/can-register", auth=False)
    return ("PASS" if json.loads(t).get("register") is False else "FAIL"), f"register={json.loads(t).get('register')}"


def api():
    code, t = http("GET", "/api/public/v1/integrations")
    if code != 200:
        return "FAIL", f"HTTP {code}"
    chans = json.loads(t)
    off = [c["name"] for c in chans if c.get("disabled")]
    return ("WARN" if off else "PASS"), f"{len(chans)} channels" + (f", disabled: {off}" if off else "")


def mcp():
    code, t = http("POST", "/api/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"})
    tools = [x["name"] for x in json.loads(t)["result"]["tools"]] if code == 200 else []
    ok = "integrationSchedulePostTool" in tools
    return ("PASS" if ok else "FAIL"), f"HTTP {code}, {len(tools)} tools"


def draft_roundtrip():
    code, t = http("GET", "/api/public/v1/integrations")
    integ = json.loads(t)[0]["id"]
    when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    code, t = http("POST", "/api/public/v1/posts", {
        "type": "draft", "date": when, "shortLink": False, "tags": [],
        "posts": [{"integration": {"id": integ}, "value": [{"content": "smoke test draft", "image": []}],
                   "settings": {}}]})
    if code not in (200, 201):
        return "FAIL", f"create HTTP {code}: {t[:120]}"
    post_id = json.loads(t)[0]["postId"]
    code, _ = http("DELETE", f"/api/public/v1/posts/{post_id}")
    return ("PASS" if code == 200 else "FAIL"), f"draft created and deleted (HTTP {code})"


def containers():
    rows = remote("docker ps -a --format '{{.Names}}|{{.Status}}'").splitlines()
    want = ["postiz", "postiz-postgres", "postiz-redis", "postiz-guard", "temporal",
            "temporal-postgresql", "temporal-elasticsearch"]
    st = dict(r.split("|", 1) for r in rows if "|" in r)
    bad = [n for n in want if not st.get(n, "").startswith("Up") or "unhealthy" in st.get(n, "")]
    return ("FAIL" if bad else "PASS"), (f"not healthy: {bad}" if bad else f"{len(want)} containers up")


def worker():
    out = remote("docker exec temporal temporal task-queue describe --task-queue main --address temporal:7233")
    polling = out.count("UNVERSIONED  activity") >= 2 and "workflow " in out.split("Pollers:")[-1]
    return ("PASS" if polling else "FAIL"), ("worker polling the main queue" if polling else "no worker polling")


def channels():
    rows = sql('select name, "refreshNeeded", "inBetweenSteps", disabled from "Integration" '
               'where "deletedAt" is null and ("refreshNeeded" or "inBetweenSteps" or disabled)')
    bad = [r.split("|")[0] for r in rows.splitlines() if r]
    return ("WARN" if bad else "PASS"), (f"need attention: {bad}" if bad else "no channel needs reconnecting")


def posts():
    err = sql('select count(*) from "Post" where state=\'ERROR\' and "deletedAt" is null '
              'and "publishDate" > now() - interval \'7 days\'')
    late = sql('select count(*) from "Post" where state=\'QUEUE\' and "deletedAt" is null '
               'and "parentPostId" is null and "publishDate" < now() - interval \'20 minutes\'')
    if not err.isdigit() or not late.isdigit():
        return "FAIL", "could not query posts"
    bad = int(err) + int(late)
    return ("WARN" if bad else "PASS"), f"{err} failed in last 7 days, {late} stuck in queue"


def backup():
    age = remote("f=$(ls -t /opt/postiz/backups/postiz-*.dump 2>/dev/null | head -1); "
                 "[ -n \"$f\" ] && echo $(( ($(date +%s) - $(stat -c %Y \"$f\")) / 3600 ))")
    if not age:
        return "FAIL", "no database backup found"
    return ("PASS" if int(age) <= 26 else "FAIL"), f"newest database backup {age} h old"


def disk():
    use = int(remote("df --output=pcent / | tail -1").strip().rstrip("%"))
    return ("PASS" if use < 90 else "WARN"), f"disk {use} % used"


def alerts():
    env = remote("docker exec postiz-guard env")
    on = [k for k in ("TELEGRAM_BOT_TOKEN", "ALERT_WEBHOOK_URL")
          if any(l.startswith(k + "=") and len(l) > len(k) + 1 for l in env.splitlines())]
    return ("PASS" if on else "WARN"), (f"alerts via {on}" if on else "guard runs but has no alert channel")


check("HTTPS certificate", tls)
check("web app", web)
check("registration closed", registration)
check("public API + channels", api)
check("MCP endpoint", mcp)
check("draft create/delete", draft_roundtrip)
if a.ssh:
    check("containers", containers)
    check("publishing worker", worker)
    check("channel tokens", channels)
    check("posts", posts)
    check("backup", backup)
    check("disk", disk)
    check("failure alerts", alerts)
print(f"\n{results.count('PASS')} pass, {results.count('WARN')} warn, {results.count('FAIL')} fail")
raise SystemExit(1 if "FAIL" in results else 0)
