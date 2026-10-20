# report-issue: "Report an issue" from the app and the website

Stores a bug report, idea or question (`issue_reports`) and e-mails the owner. Everything a user, a website author and the
owner need to know is in **`docs/REPORTING.md`** (the request and answer format, the limits, the set-up, the secrets).

Layout: `index.ts` is the only Deno file (serve, secrets, the database client). All logic is plain TypeScript that Node
tests: `handler.ts` (order of work, with injected fetch, database, clock, secrets and log), `validate.ts`, `sanitise.ts`,
`turnstile.ts`, `auth.ts` (checks the signed-in user with Supabase), `limits.ts`, `cors.ts`, `notify.ts` (the owner's
e-mail, sent through `../_shared/mail.ts`), `github.ts` (optional private-repo copy). `harness.mjs` holds the test fakes.

```bash
npm run test:unit        # runs *.test.mjs here and elsewhere (no Deno, no network)
supabase functions deploy report-issue --no-verify-jwt
```

Logs hold event names, outcomes and codes only: never the text, an address, a token, an IP or a secret (a test asserts it).
