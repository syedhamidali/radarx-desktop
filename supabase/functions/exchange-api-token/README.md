# exchange-api-token and log-activity: personal access tokens for automation

Two Edge Functions that let a batch job, cron entry, CI run or cluster node use radarx without a password or a
browser. Design, limits and the threat model are in `docs/ACCOUNTS.md` ("Access tokens for automation"); this page
is the **owner's set-up**.

* `exchange-api-token` takes a personal access token (`rxd_pat_` + 43 characters, created in the app under
  *Account > Access tokens*) and answers with a **one-hour entitlement token**: an ES256 JWT signed with a private
  key that exists only as a function secret. It is **not** a Supabase token, so row-level security never accepts it;
  the program checks it offline against a public key built into the code.
* `log-activity` takes a batch of activity events with that entitlement token, applies the same allow-list and caps as
  the database (`log-activity/allowlist.json`, checked against `python/engine/account/activity.py` and the migration
  by `python/tests/test_account_activity_allowlist.py`) and inserts them for that account with the service role.
* Shared code: `../_shared/entitlement.ts`. Logic and tests: `exchange.ts` / `activity.ts` and their `*.test.mjs`
  (`npm run test:unit`; they run under plain Node, no Deno needed). `index.ts` only wires Deno, the database and HTTP.

## Set-up (owner, once; you type nothing secret into a chat or a file in the repository)

1. **Apply the migration** (`supabase db push`): it creates `api_tokens`, the view `my_api_tokens` and the functions
   `create_api_token`, `revoke_api_token`, `use_api_token`, `api_token_rate_hit`, `purge_api_tokens`.
2. **Make the signing key pair** on your own computer:

   ```bash
   python scripts/make_entitlement_key.py --out ~/radarx-entitlement-private.pem
   ```

   It writes the PRIVATE key to that file (mode 0600, never inside the repository, never overwriting) and prints the
   PUBLIC key as JSON. It never prints the private key.
3. **Put the public key into the program**: add the printed JSON object to the list in `ENTITLEMENT_SEED_JWKS` in
   `python/engine/account/jwks_seed.py` and release. Until then access tokens report "not set up yet" and nothing
   else changes. (For a rotation, add the new key, keep the old one until its last token and the 30-day offline
   allowance have passed, then remove it.)
4. **Set the secret**, then delete the file (keep a copy in a password manager if you want to be able to set it again):

   ```bash
   supabase secrets set ENTITLEMENT_PRIVATE_KEY="$(cat ~/radarx-entitlement-private.pem)"
   rm -P ~/radarx-entitlement-private.pem        # Linux: shred -u
   ```

   `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are provided to the functions by Supabase itself; do not set them.
5. **Deploy both functions** without the Supabase JWT check (the callers have no Supabase token; the access token or the
   entitlement token is the credential; `supabase/config.toml` says the same):

   ```bash
   supabase functions deploy exchange-api-token log-activity --no-verify-jwt
   ```

6. **Check** with a real token from the app: `printf '%s\n' "$TOKEN" | rxd login --token`, then `rxd whoami`.
   `supabase functions logs exchange-api-token` shows only outcomes: no token, no hash, no address, no key is ever logged.

## What the functions answer

| Call | Answer |
|---|---|
| exchange, good token | `200 {"entitlement_token", "expires_at", "token_name", "token_expires_at"}`; the token lasts one hour or until the access token expires, whichever is first |
| exchange, unknown / revoked / expired / malformed / banned account | **the same** `401 {"error":"invalid_token"}` (no oracle) |
| too many requests | `429 {"error":"rate_limited"}`: 300 per client address per 10 minutes, 600 per token (by hash) per hour; counted before the lookup |
| database or secret trouble | `503 {"error":"unavailable"}`, never a 401, so a client never concludes a token is bad because the server is not |
| log-activity | `200 {"inserted": n}`; `400` for a bad event (the client splits the batch, as it does for the table), `401` for a bad/expired entitlement token, `429` over the caps |

The per-address limit uses the platform's `cf-connecting-ip` header, else the last `x-forwarded-for` entry. A cluster
behind one address that starts more than 300 jobs in ten minutes will see `429` and retry; that limit is a constant in
`exchange.ts`.

## Not verified here

No Deno is installed on the development machine and no function was run on Supabase: the logic is tested under Node
with fakes, the key and token formats against the Python engine (`python/tests/test_account_entitlement_functions.py`),
and the SQL on an in-process Postgres. The first real deployment is the first run of `index.ts`; check step 6.
