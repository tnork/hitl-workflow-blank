# Document Review — Quickstart

Human-in-the-loop review UI. A shipping label has already been parsed and
extracted, so you can run this and review a real document immediately — no API
key, no database, no cloud account.

## Run it

Requires **Python 3.10+**.

```bash
cd hitl-workflow-blank

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

ADMIN_PASSWORD='choose-a-password-12-chars-min' python3 web_app.py
```

Open **http://localhost:8080** and sign in as `admin` with the password you set.

> If you omit `ADMIN_PASSWORD`, a strong one is generated and printed to the
> terminal **once** on first run. Copy it. There is no default password.

## Try the review flow

1. The task board shows one entity, **TYLER NORKUS**. Click the row.
2. The shipping label opens with its ten extracted fields on the right.
3. Fields ADE grounded confidently are pre-approved (light green). Anything else
   is grey and needs your click.
4. Hover the document to magnify it. Click a field to highlight where its value
   came from on the page.
5. Correct any value by typing over it.
6. Use the **search box** in the toolbar to find text in the document: type a
   term, press Enter to jump to it, Enter again for the next hit.
7. When every field is green, a dialog asks **"Verify document?"** — *Verify* or
   *Not yet*. Nothing is ever signed off automatically; you decide. Choosing
   *Not yet* leaves the Verify button available for later.
8. On verify, the counters update (In Review → Complete) and the file moves to
   `docs_outbox/`.

Admin menu (top-left hamburger): **Settings** to change the confidence threshold
that decides which fields auto-approve, and **Users** to add reviewers.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Add your own documents

Needs a LandingAI ADE key (https://va.landing.ai).

```bash
echo 'VISION_AGENT_API_KEY=your-key' > .env

# Name files <group>_<doc_type>.pdf — the suffix picks the schema.
cp mylabel.pdf docs_inbox/acme1_shipping_label.pdf

python3 web_app.py --parse --all    # parse via ADE
python3 extract_docs.py             # extract the fields
python3 web_app.py                  # review them
```

## Use a different document type

Everything domain-specific lives in **`schema.py`**. Add a doc type there, drop a
JSON extraction schema in `schemas/`, and the UI adapts — no app code to touch.
See `CLAUDE.md` for the full developer guide.

## What's in the box

| Path | What |
|---|---|
| `schema.py` | The document model. Start here. |
| `core.py` | Pipeline config, paths, document state, persistence |
| `web_app.py` | Flask app: parsing, API, serving |
| `extract_docs.py` | ADE field extraction |
| `templates/` | The UI (self-contained HTML) |
| `docs_inbox/` | Documents waiting for review |
| `docs_outbox/` | Documents signed off |
| `tests/` | 39 tests |

## Notes

- Review decisions are held in memory and are lost on restart. Set `SUPABASE_URL`
  and `SUPABASE_SERVICE_ROLE_KEY` (and run `supabase/migrations/*.sql`) to persist
  them with a full field-level audit trail.
- Users and settings are stored in `users.json` / `settings.json`, created on
  first run.
- Run behind HTTPS in production and set `SESSION_COOKIE_SECURE=1`. If you're
  behind a reverse proxy, set `TRUST_PROXY_HEADERS=1` so login throttling reads
  the real client IP.
