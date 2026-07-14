---
title: HITL Document Review
emoji: 📄
colorFrom: gray
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Human-in-the-loop document review built on LandingAI ADE
---

# Human-in-the-loop Document Review

LandingAI ADE parses your documents and extracts structured fields. A Flask review
board puts a human in front of every field: what the AI grounded confidently is
pre-approved, everything else needs a person to look at it and click. Nothing is
signed off automatically.

**All domain configuration lives in `schema.py`.** That is the only file you edit
to point this at a new document type. No frontend code required.

Currently configured for **shipping labels** as a worked example. A sample label
ships with the repo, already parsed and extracted, so you can run it and review a
real document immediately — no API key, no database.

The UI is deliberately unbranded (white / grey / black). To skin it for a customer,
change the CSS variables in the `:root` block of `templates/index.html` and
`templates/login.html`, and swap `static/logo-mark.png`. Nothing else hardcodes a
colour.

## Run it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

ADMIN_PASSWORD='choose-a-password-12-chars-min' python3 web_app.py
# → http://localhost:8080   (or set PORT=8000)
```

Sign in as `admin` with the password you set.

> There is **no default password**. If you omit `ADMIN_PASSWORD`, a strong one is
> generated and printed to the terminal **once** on first run. Copy it.

See [QUICKSTART.md](QUICKSTART.md) for the guided walkthrough.

## How it works

**Pipeline:** `docs_inbox/*.pdf` → ADE Parse → `parse_results/docs/` → ADE Extract → `extract_results/docs/` → Flask Review UI → `docs_outbox/`

1. **Parse** — each PDF goes through ADE's `dpt-2-latest` model, returning structured
   Markdown, chunk-level grounding (bounding box + confidence per segment), and table
   cell grounding.
2. **Extract** — ADE reads the Markdown and returns typed field values with
   `references` back to chunk IDs, using the schemas in `schema.py`.
3. **Ground** — references resolve to `{page, box}` and render as SVG overlays on the
   document. Fields with no box fall back to text search (exact → numeric → token).
4. **Review** — a field is pre-approved only if ADE grounded it at or above the
   confidence threshold **and** actually extracted a value. Everything else is grey
   and needs a human click. Correct any value by typing over it.
5. **Sign off** — when every field is green, a dialog asks *"Verify document?"* —
   **Verify** or **Not yet**. **There is no auto-verification.** On verify the review
   is persisted and the file moves to `docs_outbox/`.

### In the review screen

- **Search** — type in the toolbar search box and press Enter to find text in the
  document; Enter again for the next hit, Shift+Enter for the previous, Cmd/Ctrl+F to
  focus it. Hits are highlighted in gold.
- **Magnifier** — hover the document for a zoom lens.
- **Grounding** — click a field to highlight where its value came from on the page.
- **Threshold** — hamburger → Settings sets the confidence bar that decides which
  fields auto-approve.

## Verification is server-authoritative

The browser cannot talk a document into being verified.

- `verified_by` comes from the session and `verified_at` from the server clock; both
  are ignored if the client sends them.
- Override keys are whitelisted against `schema.py`. Unknown keys are rejected.
- The server independently recomputes which fields still need a human approval and
  rejects the request (409) if any is missing.
- The file moves to the outbox **only after** the write is confirmed. A failed write
  returns 503 and leaves the document in the inbox.
- Re-verifying is idempotent: the first sign-off is the one of record.

The audit trail records who changed what, on every save — not just on sign-off — and
which fields a human had to explicitly vouch for.

## Adapting to your documents

Edit **`schema.py`**:

```python
DOC_TYPES = {
    "intake_form": "Intake Form",
}

FIELD_LABELS = {
    "intake_form": {
        "case_number": "Case Number",
        "entity_name": "Entity Name",
    },
}

EXTRACTION_SCHEMAS = {
    "intake_form": json.loads((_SCHEMAS_DIR / "intake-form-schema.json").read_text()),
}

ENTITY_NAME_FIELDS = ["entity_name"]   # what the task board shows as the entity
GROUP_PREFIX_PARTS = 1                 # how many stem segments form a group
```

Name files `<group_key>_<doc_type>.pdf` — the suffix selects the schema
(e.g. `acme1_intake_form.pdf`). Then:

```bash
echo 'VISION_AGENT_API_KEY=your-key' > .env
cp your_docs/*.pdf docs_inbox/
python3 web_app.py --parse --all    # parse via ADE
python3 extract_docs.py             # extract fields
python3 web_app.py                  # review
```

The app validates `schema.py` at startup and refuses to boot if it is inconsistent.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

43 tests, run against a temp directory tree — they never touch your real `users.json`,
`settings.json`, or inbox. They cover verification forgery, persistence-failure
rollback, file movement, CSRF, session revocation, login throttling, atomic writes,
and corrupt-file quarantine.

## Persistence

Out of the box the app runs with **no database**. Review decisions live in memory and
are lost on restart.

For durable reviews and a field-level audit trail, set `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` and run both migrations in `supabase/migrations/`. The
second one adds a transactional review+audit function and locks the tables down with
RLS.

Users and settings are stored in `users.json` / `settings.json` (gitignored), written
atomically under a lock.

## Deployment notes

- Serve over HTTPS and set `SESSION_COOKIE_SECURE=1`.
- Behind a reverse proxy, set `TRUST_PROXY_HEADERS=1` so login throttling reads the
  real client IP. Without it, `X-Forwarded-For` is ignored (it is attacker-controlled).
- **Keep gunicorn at `--workers 1`.** The login throttle, the users/settings file lock,
  and the in-memory review fallback are all per-process.

## Environment

| Variable | Purpose |
|---|---|
| `VISION_AGENT_API_KEY` | LandingAI ADE — parsing and extraction |
| `ADMIN_PASSWORD` | Password for the initial admin account (else randomly generated) |
| `PORT` | Web server port (default 8080) |
| `SECRET_KEY` | Flask session secret (auto-generated to `.secret_key` if unset) |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Optional persistence |
| `SESSION_LIFETIME_HOURS` | Session expiry, default 12 |
| `SESSION_COOKIE_SECURE` | Set to `1` when serving over HTTPS |
| `TRUST_PROXY_HEADERS` | Set to `1` only when behind a trusted proxy |
| `PIPELINE` | Active pipeline (default `docs`) |

## Files

| File | Purpose |
|---|---|
| `schema.py` | **Your document model. Start here.** |
| `core.py` | Pipeline config, paths, document state, persistence |
| `web_app.py` | Flask app: ADE parsing, API, serving |
| `extract_docs.py` | ADE field extraction |
| `templates/` | The review UI (self-contained HTML) |
| `supabase/migrations/` | Optional database schema |
| `tests/` | pytest suite |

Built on [LandingAI ADE](https://landing.ai) for document parsing and extraction.
