"""Add a Facebook Page (and its linked Instagram account) as Postiz channels without the UI.

Uses the newest valid Facebook login already stored in the database, so no OAuth
dialog is needed. Works only for Pages that login manages.

Run ON THE SERVER:
    ssh businka-prod 'python3 - --page "WishMe" --org "WishMe" [--apply]' < custom/tokens/add_meta_channel.py

Without --apply it only reports what it would add. Tokens are never printed.
"""
import argparse, hashlib, json, os, re, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--page", required=True, help="Facebook Page name or id")
ap.add_argument("--org", required=True, help="Postiz organization name")
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

GRAPH = "https://graph.facebook.com/v21.0/"
UPLOADS = "/var/lib/docker/volumes/postiz_postiz-uploads/_data"
SAFE = re.compile(r"^[A-Za-z0-9_]+$")


def sh(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()


U = sh("docker exec postiz-postgres printenv POSTGRES_USER")
D = sh("docker exec postiz-postgres printenv POSTGRES_DB")
DOMAIN = sh("grep -E '^DOMAIN=' /opt/postiz/deploy/.env | cut -d= -f2")


def psql(sql):
    out = subprocess.run(["docker", "exec", "-i", "postiz-postgres", "psql", "-U", U, "-d", D,
                          "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"],
                         input=sql, text=True, capture_output=True)
    if out.returncode:
        sys.exit("psql failed: " + out.stderr[:300])
    return [l.split("\t") for l in out.stdout.splitlines() if l]


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def graph(path, token, **params):
    params["access_token"] = token
    try:
        with urllib.request.urlopen(GRAPH + path + "?" + urllib.parse.urlencode(params), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": json.load(e).get("error", {})}


org = psql(f'select id from "Organization" where name = {lit(a.org)};')
if len(org) != 1:
    sys.exit(f"organization {a.org!r} not found")
org_id = org[0][0]

user_token = None
for (tok,) in psql('''select token from "Integration" where "deletedAt" is null
                      and "providerIdentifier" = 'instagram' order by "updatedAt" desc;'''):
    cand = tok.split("___")[-1]
    if "id" in graph("me", cand, fields="id"):
        user_token = cand
        break
if not user_token:
    sys.exit("No valid Facebook login in the database - reconnect one Instagram (Facebook Business) channel first.")
me = graph("me", user_token, fields="id")["id"]

FIELDS = ("id,name,username,access_token,picture.type(large),"
          "instagram_business_account{id,username,name,profile_picture_url}")
pages = {}
paths = ["me/accounts"]
for b in graph("me/businesses", user_token, limit=100).get("data", []):
    paths += [b["id"] + "/owned_pages", b["id"] + "/client_pages"]
for p in paths:
    for page in graph(p, user_token, fields=FIELDS, limit=100).get("data", []):
        pages.setdefault(page["id"], page)
hit = [p for p in pages.values() if a.page in (p["id"], p["name"])]
if len(hit) != 1 or not hit[0].get("access_token"):
    sys.exit(f"page {a.page!r}: {len(hit)} matches with a token among {sorted(p['name'] for p in pages.values())}")
page = hit[0]
ptoken = page["access_token"]

existing = {r[0]: r[1] for r in psql('''select i."internalId", o.name from "Integration" i
    join "Organization" o on o.id = i."organizationId" where i."deletedAt" is null;''')}


def save_picture(url):
    if not url or not a.apply:
        return ""
    data = urllib.request.urlopen(url, timeout=20).read()
    rel = time.strftime("%Y/%m/%d")
    os.makedirs(f"{UPLOADS}/{rel}", exist_ok=True)
    name = hashlib.md5(data).hexdigest() + ".jpg"
    with open(f"{UPLOADS}/{rel}/{name}", "wb") as f:
        f.write(data)
    return f"https://{DOMAIN}/uploads/{rel}/{name}"


todo = [("facebook", page["id"], page["name"], page.get("username", ""),
         ptoken, (page.get("picture") or {}).get("data", {}).get("url"))]
ig = page.get("instagram_business_account")
if ig:
    todo.append(("instagram", ig["id"], ig.get("name") or ig["username"], ig["username"],
                 ptoken + "___" + user_token, ig.get("profile_picture_url")))
else:
    print("note: the page has no linked Instagram account")

sql = "begin;\n"
for prov, internal, name, profile, token, pic in todo:
    if internal in existing:
        print(f"skip   {prov} {name} - already connected in {existing[internal]}")
        continue
    if not SAFE.match(token) or "id" not in graph(internal, ptoken, fields="id"):
        sys.exit(f"token for {prov} {name} does not work")
    print(f"add    {prov} {name} -> {a.org}")
    iid = "c" + secrets.token_hex(12)
    sql += (f'''insert into "Integration" (id, "internalId", "organizationId", name, picture,
        "providerIdentifier", type, token, "refreshToken", "tokenExpiration", profile,
        "rootInternalId", "updatedAt") values ('{iid}', {lit(internal)}, '{org_id}', {lit(name)},
        {lit(save_picture(pic))}, '{prov}', 'social', '{token}', '{user_token}',
        now() + interval '59 days', {lit(profile)}, {lit(me)}, now());\n''')
sql += "commit;\n"
if not a.apply:
    print("dry run - nothing written")
elif "insert" in sql:
    psql(sql)
    print("written")
