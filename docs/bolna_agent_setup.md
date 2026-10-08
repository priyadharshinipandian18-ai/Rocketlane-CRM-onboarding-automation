# Bolna agent setup (one time)

The code places calls with `POST /call` and reads results from the agent's **extractions**. The
agent itself — what it says, what it extracts, where it sends webhooks — is configured once in the
Bolna dashboard. Exact menu names may differ slightly from what's written here; the settings are
the same.

## 1. Account

1. Sign up at [bolna.ai](https://www.bolna.ai) (comes with $5 free credit).
2. Copy your **API key** → `BOLNA_API_KEY` in `.env`.
3. Trial accounts can only call **verified numbers**: verify your own mobile number in the dashboard.
   You'll play the AE, so also put it in `app.account_executives.phone_e164` (e.g. `+9198XXXXXXXX`).

## 2. Create the agent

Create a new voice agent (English, any voice you like) and copy its id → `BOLNA_AGENT_ID`.

**Welcome message**

```
Hi {{ae_first_name}}, this is Nova, an automated assistant from NovaCRM Customer Success. This call may be recorded. I'm calling about the {{customer_name}} deal you just closed. Do you have thirty seconds?
```

**Prompt**

```
You are Nova, an automated assistant calling on behalf of NovaCRM's Customer Success team.
You are speaking with {{ae_name}}, the Account Executive who just closed a deal with {{customer_name}}.

Your ONLY goal is to confirm whether {{customer_name}} is on the Enterprise plan or the Growth plan.

Rules:
1. Ask: "Is {{customer_name}} on the Enterprise plan or the Growth plan?"
2. Accept only an explicit answer. Never guess or infer it from deal size, seats or anything else.
3. Read the answer back: "Just to confirm, {{customer_name}} is on the <plan> plan, correct?"
4. If they say yes, thank them briefly and end the call.
5. If they correct you, read the corrected plan back and ask again.
6. If they are unsure, ask you to call back later, or you reached the wrong person,
   say you'll follow up later, thank them and end the call.
7. Keep the whole call under one minute. Do not discuss anything else.
```

The code fills `{{ae_name}}`, `{{ae_first_name}}` and `{{customer_name}}` through `user_data`
(it also sends `onboarding_id` and `attempt_no`, which the prompt doesn't need).

## 3. Extractions

Add two extractions (both **Pre-defined** answers, so the values come back exactly as written).
The names matter — the code looks for `plan_tier` and `readback_confirmed` (case and spaces don't
matter, and they can be in any category).

| Name | Options | Extraction prompt |
|---|---|---|
| `plan_tier` | `enterprise`, `growth`, `unknown` | Which plan did the Account Executive explicitly state the customer is on, AND then confirm when the assistant read it back? Answer `unknown` if they never stated a plan, were unsure, corrected it without confirming, or the call didn't reach that point. |
| `readback_confirmed` | `yes`, `no` | Did the Account Executive say yes when the assistant read the plan back to them? Answer `no` if there was no read-back or they didn't clearly agree. |

The tier is only accepted when `plan_tier` is enterprise/growth, `readback_confirmed` is `yes`,
and the extraction confidence is at least `BOLNA_MIN_CONFIDENCE` (default 0.7). Anything else is
treated as "not confirmed" and the AE is called again (up to 3 times, then a human decides).

## 4. How results reach the app (no webhook needed)

By default the app runs in **polling mode**: every `BOLNA_POLL_SECONDS` (20s) it asks Bolna
(`GET /executions/{id}`) about each call it placed that hasn't finished. When a call is done it
processes the result and continues the workflow. This only needs outbound HTTPS to
`api.bolna.ai`, so there is **nothing to configure in Bolna and no tunnel** — leave the agent's
webhook URL empty.

**Optional webhook mode** (results arrive a few seconds sooner; needs a public URL such as a
tunnel or a deployed app):
1. Generate a long random string → `BOLNA_WEBHOOK_TOKEN` in `.env`
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. Set the agent's **webhook URL** to
   `https://<your-public-host>/webhooks/bolna?token=<BOLNA_WEBHOOK_TOKEN>`.
3. Optionally set `BOLNA_ALLOWED_IPS` to Bolna's webhook IPs
   (currently `13.203.39.153,13.126.9.249,13.202.133.53`).

Polling stays on as a safety net when the webhook is used; a call is only ever processed once.

## 5. Test

Send yourself a deal-closed email (from an address in `app.account_executives`). Your phone should
ring within a few seconds. Say "Enterprise", then "yes" to the read-back. A minute or so after you
hang up (Bolna finishes post-call processing first, then the next polling pass picks it up) the Rocketlane project is created. Check
progress with:

```sql
SELECT event_type, payload, created_at FROM app.audit_events ORDER BY id DESC LIMIT 20;
```
