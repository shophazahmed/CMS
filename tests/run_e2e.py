#!/usr/bin/env python3
"""End-to-end test of all four workflows against the offline mock APIs.

Prereq (from repo root):
    export COMPOSE_FILE=docker-compose.yml:tests/docker-compose.test.yml
    scripts/bootstrap.sh --activate        # with the test .env described in README
    python3 tests/run_e2e.py

Drives workflow 02 exactly like Telegram does (signed webhook POSTs), runs the
scheduled workflows 01/03 through the n8n CLI, and asserts on the database and
on every request the mock APIs received.
"""
import json
import pathlib
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "tests/.work/requests.jsonl"
WF2 = json.loads((ROOT / "n8n/workflows/02-telegram-approval-handler.json").read_text())
TRIGGER = next(n for n in WF2["nodes"] if n["type"] == "n8n-nodes-base.telegramTrigger")
HOOK = f"http://127.0.0.1:5678/webhook/{TRIGGER['webhookId']}/webhook"
SECRET = f"{WF2['id']}_{TRIGGER['id']}"
ADMIN_CHAT = -100500
update_id = [100]
failures = []


def sh(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sql(q):
    r = sh("docker", "exec", "social-hub-postgres", "psql", "-U", "socialhub", "-d", "social_hub",
           "-tAF", "|", "-c", q)
    return [line.split("|") for line in r.stdout.strip().splitlines() if line]


def requests(since=0):
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines()[since:]]


def mark():
    return len(requests())


def tg_post(update):
    update_id[0] += 1
    update["update_id"] = update_id[0]
    req = urllib.request.Request(
        HOOK, data=json.dumps(update).encode(),
        headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        assert r.status == 200, r.status


def callback(data, message_id, chat=ADMIN_CHAT, text="(preview text)"):
    tg_post({"callback_query": {
        "id": str(uuid.uuid4()), "data": data,
        "from": {"id": 7, "is_bot": False, "first_name": "Ana", "username": "ana_ops"},
        "message": {"message_id": int(message_id), "chat": {"id": chat, "type": "supergroup"}, "text": text},
    }})


def message(text="", chat=ADMIN_CHAT, photo=None, reply_to=None):
    m = {"message_id": 5000 + update_id[0], "chat": {"id": chat, "type": "supergroup"},
         "from": {"id": 7, "is_bot": False, "first_name": "Ana", "username": "ana_ops"},
         "date": int(time.time())}
    if photo:
        m["photo"] = [{"file_id": photo + "_small", "width": 90}, {"file_id": photo, "width": 1280}]
        m["caption"] = text
    else:
        m["text"] = text
    if reply_to:
        m["reply_to_message"] = {"message_id": int(reply_to), "chat": m["chat"]}
    tg_post({"message": m})


def run_cli(wf_id):
    r = sh("docker", "exec", "-e", "N8N_RUNNERS_BROKER_PORT=5690", "social-hub-n8n",
           "n8n", "execute", f"--id={wf_id}", timeout=240)
    if "Execution error" in r.stdout + r.stderr:
        print((r.stdout + r.stderr)[-2000:])
    return r


def wait_for(fn, what, timeout=40):
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if v:
            return v
        time.sleep(1)
    return None


def check(cond, name, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def post(pid):
    rows = sql(f"select json_build_array(status, coalesce(fb_post_id,''), coalesce(tweet_id,''), "
               f"coalesce(error_message,''), coalesce(telegram_message_id,''), twitter_copy, fb_copy, "
               f"coalesce(media_url,''), regen_count::text) from content_queue where id={pid}")
    return json.loads("|".join(rows[0])) if rows else None


def calls(reqs, pred):
    return [r for r in reqs if pred(r)]


# ---------------------------------------------------------------------------
print("[0] Resetting test data")
sql("truncate content_queue, mentions, app_state restart identity")
sh("docker", "exec", "social-hub-postgres", "psql", "-U", "socialhub", "-d", "n8n", "-qc",
   "delete from execution_entity")

print("\n[1] Workflow 01: scheduled AI draft")
m0 = mark()
run_cli("shDailyAiGen0001")
pid = int(sql("select max(id) from content_queue")[0][0])
p = post(pid)
check(p and p[0] == "PENDING_APPROVAL", "draft saved as PENDING_APPROVAL", p)
rq = requests(m0)
cl = calls(rq, lambda r: r["path"] == "/v1/messages")
check(cl and cl[0]["body"]["output_config"]["format"]["type"] == "json_schema", "Claude called with JSON-schema output")
check(cl and cl[0]["body"]["model"] == "claude-sonnet-5", "copy model is claude-sonnet-5")
sm = calls(rq, lambda r: r.get("tg_method") == "sendMessage")
kb = sm and sm[-1]["body"].get("reply_markup", {}).get("inline_keyboard")
check(kb and [b["callback_data"] for b in kb[0]] == [f"a:all:{pid}", f"a:fb:{pid}", f"a:tw:{pid}"],
      "approval keyboard row 1 has Approve All / FB / X", kb)
check(kb and [b["callback_data"] for b in kb[1]] == [f"rg:{pid}", f"ed:{pid}", f"rj:{pid}"],
      "approval keyboard row 2 has Regen / Edit / Reject", kb)
check(p and p[4] != "", "telegram_message_id stored")

print("\n[2] Workflow 02 + 04: Approve All -> Facebook /feed + X /2/tweets")
m0 = mark()
callback(f"a:all:{pid}", p[4])
p = wait_for(lambda: (lambda x: x if x and x[0] in ("PUBLISHED", "FAILED") else None)(post(pid)), "publish")
check(p and p[0] == "PUBLISHED", "post PUBLISHED", p)
check(p and p[1] == "pg42_111" and p[2] == "1234567890", "fb_post_id and tweet_id stored", p)
time.sleep(2)
rq = requests(m0)
check(calls(rq, lambda r: r.get("tg_method") == "answerCallbackQuery"), "callback answered (spinner stops)")
fb = calls(rq, lambda r: r["path"].endswith("/feed"))
check(fb and fb[0]["path"] == "/v19.0/pg42/feed" and fb[0]["query"].get("access_token") == ["test-fb-token"],
      "FB Graph POST /v19.0/{page}/feed with page token")
tw = calls(rq, lambda r: r["path"] == "/2/tweets")
check(tw and tw[0].get("oauth1") == "ok", "X /2/tweets signed with valid OAuth 1.0a", tw and tw[0].get("oauth1"))
ed = calls(rq, lambda r: r.get("tg_method") == "editMessageText")
check(ed and "Published" in ed[-1]["body"].get("text", "") and "reply_markup" not in ed[-1]["body"],
      "Telegram message edited to Published and keyboard removed")

print("\n[3] Double-click Approve is ignored (no second publish)")
m0 = mark()
callback(f"a:all:{pid}", p[4])
time.sleep(6)
rq = requests(m0)
check(not calls(rq, lambda r: r["path"].endswith("/feed") or r["path"] == "/2/tweets"), "no duplicate posts")

print("\n[4] Regenerate")
run_cli("shDailyAiGen0001")
pid2 = int(sql("select max(id) from content_queue")[0][0])
p2 = post(pid2)
m0 = mark()
callback(f"rg:{pid2}", p2[4])
p2n = wait_for(lambda: (lambda x: x if x and x[0] == "PENDING_APPROVAL" and x[8] == "1" else None)(post(pid2)), "regen")
check(p2n is not None, "regenerated draft back to PENDING_APPROVAL with regen_count=1", post(pid2))
time.sleep(2)
rq = requests(m0)
cl = calls(rq, lambda r: r["path"] == "/v1/messages")
check(cl and "rejected this draft" in json.dumps(cl[0]["body"]["messages"]), "regen prompt includes previous draft")
check(len(calls(rq, lambda r: r.get("tg_method") == "sendMessage")) >= 1, "new preview sent")

print("\n[5] Edit via ForceReply")
m0 = mark()
callback(f"ed:{pid2}", post(pid2)[4])
fr = wait_for(lambda: calls(requests(m0), lambda r: r.get("tg_method") == "sendMessage"
                            and "force_reply" in json.dumps(r["body"])), "force reply")
check(bool(fr), "ForceReply prompt sent")
prompt_id = wait_for(lambda: (sql(f"select edit_prompt_message_id from content_queue where id={pid2}") or [[""]])[0][0], "prompt id")
check(bool(prompt_id), "edit prompt id stored")
m0 = mark()
message("TW: Edited tweet text #YourBrand\nFB: Edited Facebook text\nsecond line", reply_to=prompt_id)
ok = wait_for(lambda: (lambda x: x if x and x[5] == "Edited tweet text #YourBrand" else None)(post(pid2)), "edit")
check(ok and ok[6] == "Edited Facebook text\nsecond line", "TW:/FB: copy applied", post(pid2))
time.sleep(4)
check(len(calls(requests(m0), lambda r: r.get("tg_method") == "sendMessage")) >= 1, "updated preview sent")

print("\n[6] Reject")
callback(f"rj:{pid2}", post(pid2)[4])
check(wait_for(lambda: post(pid2)[0] == "REJECTED", "reject"), "post REJECTED")

print("\n[7] Team photo + caption -> Claude adapt -> draft with photo")
m0 = mark()
message("New espresso blend launches Friday", photo="PHOTO123")
pid3 = wait_for(lambda: (lambda r: int(r[0][0]) if r and r[0][0] and int(r[0][0]) > pid2 else None)(
    sql("select max(id) from content_queue where created_by='TEAM_MEMBER'")), "team draft")
check(pid3 is not None, "team draft created")
p3 = wait_for(lambda: (lambda x: x if x and x[4] else None)(post(pid3)), "preview") if pid3 else None
check(p3 and p3[7] == "tg://PHOTO123", "media stored as tg://<file_id>", p3)
rq = requests(m0)
check(calls(rq, lambda r: r.get("tg_method") == "sendPhoto" and r["body"].get("photo") == "PHOTO123"),
      "photo echoed in preview")
check(calls(rq, lambda r: r["path"] == "/v1/messages" and "espresso" in json.dumps(r["body"])),
      "Claude adapted the team text")

print("\n[8] Approve photo post -> FB /photos multipart + X media upload")
m0 = mark()
callback(f"a:all:{pid3}", p3[4])
p3 = wait_for(lambda: (lambda x: x if x and x[0] in ("PUBLISHED", "FAILED") else None)(post(pid3)), "publish")
check(p3 and p3[0] == "PUBLISHED", "photo post PUBLISHED", p3)
time.sleep(2)
rq = requests(m0)
check(calls(rq, lambda r: r.get("tg_method") == "getFile"), "downloaded photo from Telegram")
ph = calls(rq, lambda r: r["path"].endswith("/photos"))
check(ph and set(ph[0]["body"].get("_multipart_fields", [])) >= {"source", "message"},
      "FB /photos multipart with source+message", ph and ph[0]["body"])
up = calls(rq, lambda r: r["path"] == "/2/media/upload")
check(up and up[0].get("oauth1") == "ok" and "media" in up[0]["body"].get("_multipart_fields", []),
      "X media upload multipart, OAuth ok", up and (up[0].get("oauth1"), up[0]["body"]))
tw = calls(rq, lambda r: r["path"] == "/2/tweets")
check(tw and tw[0]["body"].get("media", {}).get("media_ids") == ["777"], "tweet references uploaded media",
      tw and tw[0]["body"])

print("\n[9] Security: other chats are ignored, /id answers")
m0 = mark()
callback(f"rj:{pid}", "1", chat=-999)
message("hello from a stranger", chat=-999)
message("/id", chat=-999)
time.sleep(6)
rq = requests(m0)
check(post(pid)[0] == "PUBLISHED", "stranger could not reject a post")
helps = calls(rq, lambda r: r.get("tg_method") == "sendMessage")
check(len(helps) == 1 and "-999" in helps[0]["body"]["text"], "/id replies with the chat id only",
      [h["body"].get("text", "")[:80] for h in helps])
check(not calls(rq, lambda r: r["path"] == "/v1/messages"), "stranger message did not reach Claude")

print("\n[10] Workflow 03: mention listener")
m0 = mark()
run_cli("shTwListener0003")
rows = sql("select tweet_id, status, ai_score, ai_verdict from mentions order by tweet_id")
check(rows == [["9001", "NOTIFIED", "9", "HIGH_VALUE"], ["9002", "FILTERED", "2", "SPAM"]],
      "high-value NOTIFIED, spam FILTERED", rows)
check(sql("select value from app_state where key='twitter_mentions_since_id'") == [["9002"]], "since_id saved")
rq = requests(m0)
mn = calls(rq, lambda r: r["path"].endswith("/mentions"))
check(mn and "start_time" in mn[0]["query"], "first poll uses 24h start_time")
cl = calls(rq, lambda r: r["path"] == "/v1/messages")
check(cl and all(c["body"]["model"] == "claude-haiku-4-5" for c in cl), "classifier uses claude-haiku-4-5")
rt_msg = calls(rq, lambda r: r.get("tg_method") == "sendMessage")
check(len(rt_msg) == 1 and "rt:" in json.dumps(rt_msg[0]["body"]), "one Telegram retweet prompt")
m0 = mark()
run_cli("shTwListener0003")
rq = requests(m0)
mn = calls(rq, lambda r: r["path"].endswith("/mentions"))
check(mn and mn[0]["query"].get("since_id") == ["9002"], "second poll uses since_id")
check(not calls(rq, lambda r: r.get("tg_method") == "sendMessage"), "no duplicate prompt for seen mentions")

print("\n[11] Retweet button")
mid, tmid = sql("select id, telegram_message_id from mentions where tweet_id='9001'")[0]
m0 = mark()
callback(f"rt:{mid}", tmid)
check(wait_for(lambda: sql(f"select status from mentions where id={mid}") == [["RETWEETED"]], "rt"), "mention RETWEETED")
time.sleep(2)
rt = calls(requests(m0), lambda r: r["path"] == "/2/users/42/retweets")
check(rt and rt[0].get("oauth1") == "ok" and rt[0]["body"] == {"tweet_id": "9001"},
      "POST /2/users/:id/retweets with OAuth", rt and rt[0])

print("\n[12] Facebook failure is reported, not swallowed")
sh("docker", "compose", "up", "-d", "--force-recreate", "mock", env={**__import__("os").environ, "MOCK_FAIL_FB": "1"})
time.sleep(3)
run_cli("shDailyAiGen0001")
pid4 = int(sql("select max(id) from content_queue")[0][0])
m0 = mark()
callback(f"a:fb:{pid4}", post(pid4)[4])
p4 = wait_for(lambda: (lambda x: x if x and x[0] in ("PUBLISHED", "FAILED") else None)(post(pid4)), "fail")
check(p4 and p4[0] == "FAILED" and "Mock FB failure" in p4[3], "status FAILED with Graph error message", p4)
time.sleep(2)
rq = requests(m0)
check(not calls(rq, lambda r: r["path"] == "/2/tweets"), "FB-only approval did not tweet")
ed = calls(rq, lambda r: r.get("tg_method") == "editMessageText")
check(ed and "failed" in ed[-1]["body"].get("text", ""), "Telegram shows the failure")
sh("docker", "compose", "up", "-d", "--force-recreate", "mock", env={**__import__("os").environ, "MOCK_FAIL_FB": "0"})

print("\n[13] No workflow execution ended in error")
time.sleep(3)
errs, total = (lambda r: (r.stdout.strip() or "0|0").split("|"))(sh(
    "docker", "exec", "social-hub-postgres", "psql", "-U", "socialhub", "-d", "n8n", "-tAc",
    "select count(*) filter (where status = 'error'), count(*) from execution_entity"))
check(errs == "0", f"0 errored executions out of {total}", f"{errs} errored")

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
