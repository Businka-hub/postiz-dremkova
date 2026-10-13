"""postiz-guard: alerts when Postiz silently breaks.

Checks the Postiz database every CHECK_EVERY seconds and sends one alert per
problem (Telegram and/or a generic webhook):
  - channel needs reconnect (refreshNeeded / disabled / stuck in inBetweenSteps)
  - post failed (state ERROR)
  - post stuck in QUEUE more than STUCK_MINUTES after its publish date
Sends a daily summary at SUMMARY_HOUR (server time) of the next 24 h.
"""
import json, os, time, urllib.request, datetime as dt
import psycopg

DB = os.environ["DATABASE_URL"]
EVERY = int(os.environ.get("CHECK_EVERY", "300"))
STUCK = int(os.environ.get("STUCK_MINUTES", "20"))
SUMMARY_HOUR = int(os.environ.get("SUMMARY_HOUR", "8"))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK = os.environ.get("ALERT_WEBHOOK_URL", "")
APP_URL = os.environ.get("APP_URL", "")
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"seen": {}, "summary_day": ""}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def send(text):
    print(text, flush=True)
    if TG_TOKEN and TG_CHAT:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    if WEBHOOK:
        req = urllib.request.Request(WEBHOOK, data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()


def problems(cur):
    out = []
    cur.execute('''select id, name, "providerIdentifier", disabled, "refreshNeeded", "inBetweenSteps"
                   from "Integration" where "deletedAt" is null
                   and (disabled or "refreshNeeded" or "inBetweenSteps")''')
    for iid, name, prov, dis, ref, mid in cur.fetchall():
        why = "vypnutý" if dis else ("potřebuje znovu připojit" if ref else "nedokončené připojení")
        out.append((f"ch:{iid}:{why}", f"⚠️ Kanál {name} ({prov}) – {why}. {APP_URL}/launches"))
    cur.execute('''select p.id, i.name, i."providerIdentifier", p."publishDate", coalesce(p.error,'')
                   from "Post" p join "Integration" i on i.id = p."integrationId"
                   where p.state = 'ERROR' and p."deletedAt" is null and p."parentPostId" is null
                   and p."updatedAt" > now() - interval '7 days' ''')
    for pid, name, prov, when, err in cur.fetchall():
        out.append((f"err:{pid}", f"❌ Post selhal – {name} ({prov}), {when:%d.%m. %H:%M} UTC: {err[:300] or 'bez chybové zprávy'}"))
    cur.execute('''select p.id, i.name, i."providerIdentifier", p."publishDate"
                   from "Post" p join "Integration" i on i.id = p."integrationId"
                   where p.state = 'QUEUE' and p."deletedAt" is null and p."parentPostId" is null
                   and p."publishDate" < now() - make_interval(mins => %s)
                   and p."publishDate" > now() - interval '7 days' ''', (STUCK,))
    for pid, name, prov, when in cur.fetchall():
        out.append((f"stuck:{pid}", f"⏳ Post visí ve frontě – {name} ({prov}), měl jít ven {when:%d.%m. %H:%M} UTC."))
    return out


def summary(cur):
    cur.execute('''select i.name, count(*) from "Post" p join "Integration" i on i.id = p."integrationId"
                   where p.state = 'QUEUE' and p."deletedAt" is null and p."parentPostId" is null
                   and p."publishDate" between now() and now() + interval '24 hours'
                   group by i.name order by 2 desc''')
    rows = cur.fetchall()
    if not rows:
        return "📅 Postiz: na příštích 24 h není nic naplánováno."
    return "📅 Postiz – příštích 24 h:\n" + "\n".join(f"• {n}: {c}" for n, c in rows)


def tick(state):
    with psycopg.connect(DB, connect_timeout=10) as conn, conn.cursor() as cur:
        current = problems(cur)
        keys = {k for k, _ in current}
        for k, msg in current:
            if k not in state["seen"]:
                send(msg)
                state["seen"][k] = msg
        # forget resolved problems so a relapse alerts again
        for k in list(state["seen"]):
            if k not in keys:
                if k.startswith("ch:"):
                    name = state["seen"][k].split(" – ")[0].replace("⚠️ Kanál ", "")
                    send(f"✅ Kanál {name} je zase v pořádku.")
                del state["seen"][k]
        today = dt.date.today().isoformat()
        if dt.datetime.now().hour >= SUMMARY_HOUR and state.get("summary_day") != today:
            send(summary(cur))
            state["summary_day"] = today


def main():
    state = load_state()
    while True:
        try:
            tick(state)
            save_state(state)
        except Exception as e:  # keep running; a DB restart must not kill the guard
            print("guard error:", e, flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
