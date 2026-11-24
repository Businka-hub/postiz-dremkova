"""Repair all Facebook / Instagram (Facebook Business) channels from ONE fresh login.

Meta invalidates every token of a user at once (password change, security reset:
error 190 / subcode 460). Postiz then needs a manual reconnect per channel. This
script takes the newest valid user token stored in the database (any Instagram
channel reconnected in the UI holds it as the second half of its token), asks
Meta for fresh Page tokens and writes them to every dead channel.

Run ON THE SERVER (it talks to the postiz-postgres container):
    ssh businka-prod 'python3 - --dry-run' < custom/tokens/refresh_meta_tokens.py
    ssh businka-prod 'python3 - --apply'   < custom/tokens/refresh_meta_tokens.py

Tokens are never printed. --apply first saves the old values to
/opt/postiz/backups/tokens-<time>.json (chmod 600).
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

GRAPH = "https://graph.facebook.com/v21.0/"
APPLY = "--apply" in sys.argv
SAFE = re.compile(r"^[A-Za-z0-9_]+$")


def sh(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()


U = sh("docker exec postiz-postgres printenv POSTGRES_USER")
D = sh("docker exec postiz-postgres printenv POSTGRES_DB")


def psql(sql):
    out = subprocess.run(["docker", "exec", "-i", "postiz-postgres", "psql", "-U", U, "-d", D,
                          "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"],
                         input=sql, text=True, capture_output=True)
    if out.returncode:
        sys.exit("psql failed: " + out.stderr[:300])
    return [l.split("\t") for l in out.stdout.splitlines() if l]


def graph(path, token, **params):
    params["access_token"] = token
    try:
        with urllib.request.urlopen(GRAPH + path + "?" + urllib.parse.urlencode(params), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": json.load(e).get("error", {})}


def alive(token, internal_id):
    return "id" in graph(internal_id, token, fields="id")


rows = psql('''select i.id, o.name, i."providerIdentifier", i.name, i."internalId", i.token,
                      coalesce(i."refreshToken", ''), coalesce(i."tokenExpiration"::text, ''),
                      i."refreshNeeded"
               from "Integration" i join "Organization" o on o.id = i."organizationId"
               where i."deletedAt" is null and i."providerIdentifier" in ('facebook', 'instagram')
               order by i."updatedAt" desc;''')

# newest working user token = second half of an Instagram channel token
user_token = None
for r in rows:
    if r[2] == "instagram" and "___" in r[5]:
        cand = r[5].split("___")[1]
        if "id" in graph("me", cand, fields="id"):
            user_token, source = cand, r[3]
            break
if not user_token:
    sys.exit("No valid user token in the database. Reconnect ONE Instagram (Facebook Business) "
             "channel in Postiz (Preferences -> reconnect), then run this again.")
print("fresh login taken from channel:", source)

pages = {}
FIELDS = "id,name,access_token,instagram_business_account"


def collect(path):
    url_params = dict(fields=FIELDS, limit=100)
    data = graph(path, user_token, **url_params)
    for p in data.get("data", []):
        pages.setdefault(p["id"], p)


collect("me/accounts")
for b in graph("me/businesses", user_token, limit=100).get("data", []):
    collect(b["id"] + "/owned_pages")
    collect(b["id"] + "/client_pages")
by_ig = {p["instagram_business_account"]["id"]: p for p in pages.values() if p.get("instagram_business_account")}

updates, backup, missing = [], [], []
for iid, org, prov, name, internal, token, rtoken, exp, need in rows:
    current = token.split("___")[0]
    if alive(current, internal) and need == "f":
        print(f"ok       {org} | {prov} | {name[:30]}")
        continue
    page = pages.get(internal) if prov == "facebook" else by_ig.get(internal)
    page_token = (page or {}).get("access_token") or (
        graph(page["id"], user_token, fields="access_token").get("access_token") if page else None)
    if not page_token:
        missing.append(f"{org} | {prov} | {name[:30]}")
        print(f"MISSING  {org} | {prov} | {name[:30]}  (this login does not manage the page)")
        continue
    new_token = page_token if prov == "facebook" else page_token + "___" + user_token
    if not (SAFE.match(new_token) and alive(page_token, internal)):
        missing.append(f"{org} | {prov} | {name[:30]}")
        print(f"BAD      {org} | {prov} | {name[:30]}  (new token does not work)")
        continue
    print(f"repair   {org} | {prov} | {name[:30]}")
    backup.append(dict(id=iid, token=token, refreshToken=rtoken, tokenExpiration=exp, refreshNeeded=need))
    updates.append((iid, new_token))

print(f"\n{len(updates)} to repair, {len(missing)} not covered")
if not APPLY or not updates:
    print("dry run - nothing written" if not APPLY else "nothing to do")
    sys.exit(1 if missing else 0)

path = f"/opt/postiz/backups/tokens-{time.strftime('%Y%m%d-%H%M%S')}.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(backup, f)
print("old values saved to", path)

sql = "begin;\n"
for iid, tok in updates:
    assert SAFE.match(iid) and SAFE.match(tok)
    sql += (f'''update "Integration" set token = '{tok}', "refreshToken" = '{user_token}', '''
            f'''"tokenExpiration" = now() + interval '59 days', "refreshNeeded" = false, '''
            f'''"updatedAt" = now() where id = '{iid}';\n''')
sql += "commit;\n"
psql(sql)

bad = 0
for iid, _org, prov, name, internal, *_ in rows:
    tok = psql(f'''select token from "Integration" where id = '{iid}';''')[0][0].split("___")[0]
    if not alive(tok, internal):
        bad += 1
        print("STILL DEAD:", prov, name[:30])
print("verified: all channels answer" if not bad else f"{bad} channels still dead")
sys.exit(1 if bad or missing else 0)
