"""Create a separate Postiz organization (workspace) for a client, owned by an existing user.

Postiz has no UI for a second organization of the same user, so it is created in the database,
the same way as "Gutiva" on 14 Oct 2026. One client = one organization (never mix accounts).

Usage:
  python -I create_org.py --name Newtone --env-var POSTIZ_NEWTONE_API_KEY [--ssh businka-prod] [--dry-run]

--user     e-mail of the owner (SUPERADMIN of the new organization), default anastasiya.drs@gmail.com
--env-var  name under which the organization API key is saved to --env (the key is never printed)
Runs the nightly backup script first, then one transaction (INSERT Organization + UserOrganization).
Refuses to run when an organization with the same name already exists.
"""
import argparse, json, os, re, secrets, subprocess, uuid

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)
ap.add_argument("--env-var", required=True)
ap.add_argument("--user", default="anastasiya.drs@gmail.com")
ap.add_argument("--ssh", default="businka-prod")
ap.add_argument("--env", default=os.path.expanduser("~/.claude/.env"))
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

if not re.fullmatch(r"[A-Z][A-Z0-9_]*", a.env_var):
    raise SystemExit("--env-var must look like POSTIZ_CLIENT_API_KEY")


def lit(s):
    return "'" + s.replace("'", "''") + "'"


def psql(sql):
    """Run SQL from stdin (keeps the API key out of the process list) and return tuples-only output."""
    r = subprocess.run(["ssh", a.ssh, "docker exec -i postiz-postgres psql -U postiz -d postiz "
                        "-v ON_ERROR_STOP=1 -tA"], input=sql, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit(f"psql failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


user_id = psql(f'select id from "User" where email = {lit(a.user)};')
if not re.fullmatch(r"[0-9a-f-]{36}", user_id):
    raise SystemExit(f"user {a.user} not found")
taken = psql(f'select count(*) from "Organization" where lower(name) = lower({lit(a.name)}) and "deletedAt" is null;')
if taken != "0":
    raise SystemExit(f"organization '{a.name}' already exists")
env_text = open(a.env, encoding="utf-8").read() if os.path.exists(a.env) else ""
if re.search(rf"^{a.env_var}=", env_text, re.M):
    raise SystemExit(f"{a.env_var} already exists in {a.env}")

org_id, key = str(uuid.uuid4()), secrets.token_hex(32)
print(f"owner {a.user} ({user_id}), new organization '{a.name}' id {org_id}")
if a.dry_run:
    raise SystemExit("dry run, nothing written")

print(subprocess.run(["ssh", a.ssh, "/opt/postiz/backup.sh"], capture_output=True, text=True,
                     timeout=600, check=True).stdout.strip())
# Same flags as the organizations created by Postiz registration (allowTrial/isTrailing true, shortlink ASK).
psql(f"""begin;
insert into "Organization" (id, name, "apiKey", "allowTrial", "isTrailing", shortlink, "createdAt", "updatedAt")
values ({lit(org_id)}, {lit(a.name)}, {lit(key)}, true, true, 'ASK', now(), now());
insert into "UserOrganization" (id, "userId", "organizationId", disabled, role, "createdAt", "updatedAt")
values ({lit(str(uuid.uuid4()))}, {lit(user_id)}, {lit(org_id)}, false, 'SUPERADMIN', now(), now());
commit;""")

with open(a.env, "a", encoding="utf-8") as f:
    f.write(("" if env_text.endswith("\n") or not env_text else "\n") + f"{a.env_var}={key}\n")
print(json.dumps({"org_created": org_id, "name": a.name, "api_key_saved_as": a.env_var}))
