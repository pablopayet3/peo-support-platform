"""Zendesk configuration as code: plan, then apply.

Reads config.yaml, compares it with the account, and creates what is missing. Existing objects are
matched by title or name and left as they are, so the tool is safe to run again (idempotent).
Credentials come from the environment: ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN.
"""
import json
import os
import re
import sys

import httpx
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


class Zendesk:
    def __init__(self):
        sub, email, token = (os.environ[k] for k in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"))
        self.c = httpx.Client(base_url=f"https://{sub}.zendesk.com/api/v2", auth=(f"{email}/token", token), timeout=20)

    def list(self, path: str, key: str) -> list[dict]:
        out, url = [], f"/{path}.json"
        while url:
            data = self.c.get(url).raise_for_status().json()
            out += data[key]
            url = data.get("next_page")
        return out

    def post(self, path: str, body: dict) -> dict:
        return self.c.post(f"/{path}.json", json=body).raise_for_status().json()


def expand_env(s: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], s)


def run(apply: bool) -> int:
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml")))
    zd = Zendesk()
    changes = 0

    def do(kind: str, name: str, fn):
        nonlocal changes
        changes += 1
        print(f"{'create' if apply else 'would create'} {kind}: {name}")
        return fn() if apply else None

    fields = {f["title"]: f for f in zd.list("ticket_fields", "ticket_fields")}
    for f in cfg["ticket_fields"]:
        if f["title"] not in fields:
            body = {"type": f["type"], "title": f["title"]}
            if f.get("options"):
                body["custom_field_options"] = f["options"]
            res = do("field", f["title"], lambda b=body: zd.post("ticket_fields", {"ticket_field": b}))
            if res:
                fields[f["title"]] = res["ticket_field"]

    groups = {g["name"]: g for g in zd.list("groups", "groups")}
    for name in cfg["groups"]:
        if name not in groups:
            res = do("group", name, lambda n=name: zd.post("groups", {"group": {"name": n}}))
            if res:
                groups[name] = res["group"]

    hooks = {h["name"]: h for h in zd.list("webhooks", "webhooks")}
    for h in cfg["webhooks"]:
        if h["name"] not in hooks:
            body = {"name": h["name"], "endpoint": expand_env(h["endpoint"]), "http_method": "POST",
                    "request_format": "json", "status": "active", "subscriptions": ["conditional_ticket_events"]}
            res = do("webhook", h["name"], lambda b=body: zd.post("webhooks", {"webhook": b}))
            if res:
                hooks[h["name"]] = res["webhook"]

    def resolve(value):
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r"\{field:([^}]+)\}", lambda m: f"custom_fields_{fields[m.group(1)]['id']}", value)
        value = re.sub(r"\{group:([^}]+)\}", lambda m: str(groups[m.group(1)]["id"]), value)
        return re.sub(r"\{webhook:([^}]+)\}", lambda m: hooks[m.group(1)]["id"], value)

    cats = {c["name"]: c for c in zd.c.get("/trigger_categories.json").raise_for_status().json()["trigger_categories"]}
    cat = cats.get(cfg["trigger_category"])
    if cat is None:
        res = do("trigger category", cfg["trigger_category"],
                 lambda: zd.post("trigger_categories", {"trigger_category": {"name": cfg["trigger_category"]}}))
        cat = res["trigger_category"] if res else {"id": "(new)"}

    existing = {t["title"] for t in zd.list("triggers", "triggers")}
    for t in cfg["triggers"]:
        if t["title"] in existing:
            continue
        missing = [n for n in re.findall(r"\{(?:field|webhook):([^}]+)\}", json.dumps(t)) if n not in fields and n not in hooks]
        if missing and apply:
            print(f"skip {t['title']}: unresolved {missing}")
            continue
        body = {"title": t["title"], "description": t.get("description", ""), "category_id": str(cat["id"]),
                "conditions": {"all": [resolve(c) for c in t.get("all", [])], "any": [resolve(c) for c in t.get("any", [])]},
                "actions": [resolve(a) for a in t["actions"]]} if not missing else {}
        do("trigger", t["title"], lambda b=body: zd.post("triggers", {"trigger": b}))

    print(f"{changes} change(s) {'applied' if apply else 'planned'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("plan", "apply"):
        sys.exit("usage: python apply.py plan|apply")
    sys.exit(run(sys.argv[1] == "apply"))
