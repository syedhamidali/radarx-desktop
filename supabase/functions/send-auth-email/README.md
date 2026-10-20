# send-auth-email: Resend first, Mailjet as the fallback

Supabase Auth "Send Email" hook for radarx. Supabase calls this function (a signed webhook) instead of sending the
e-mail itself; the function builds the short code message (`mail.ts`) and sends it with **Resend**; if Resend refuses,
fails or does not answer within 8 s, **Mailjet** sends it. Only the e-mail kinds radarx uses are supported (`signup`,
`recovery`, `magiclink`, `reauthentication`); anything else is refused, not guessed.

Logs show the e-mail kind, the provider that sent it and the outcome of each attempt. **Never** an address, a code or a key.

## Set-up (owner, once; you type the secrets)
```bash
supabase link --project-ref bfyimkgvnqiqedcwprpi        # already done if you applied the migrations
supabase secrets set \
  MAIL_FROM='Radarx <no-reply@labshope.com>' \
  RESEND_API_KEY='...' \
  MAILJET_API_KEY='...' MAILJET_SECRET_KEY='...'          # optional: without them there is no fallback
supabase functions deploy send-auth-email --no-verify-jwt   # Supabase calls it with a signed webhook, not a user token
```
Then in the dashboard: *Authentication > Hooks > Send Email*, choose **HTTPS**, the function's URL
(`https://bfyimkgvnqiqedcwprpi.supabase.co/functions/v1/send-auth-email`), generate the secret, and store it:
```bash
supabase secrets set SEND_EMAIL_HOOK_SECRET='v1,whsec_...'
```
(Or enable `[auth.hook.send_email]` in `supabase/config.toml` and run `supabase config push`. Only after the function
is deployed: while the hook is enabled Supabase sends no e-mail itself.)

## Options (secrets or env of the function)
| Name | Meaning |
|---|---|
| `MAIL_FROM` | Sender, `Name <address>` or a bare address. Must be a sender verified at **both** providers. Required |
| `RESEND_API_KEY` | Resend API key (sending access only) |
| `MAILJET_API_KEY`, `MAILJET_SECRET_KEY` | Mailjet key pair |
| `MAIL_ORDER` | `resend,mailjet` (default) or `mailjet,resend`; a provider without keys is skipped |
| `SEND_EMAIL_HOOK_SECRET` | The webhook secret from the dashboard (`v1,whsec_...`) |

## Test it
1. Sign up in the app with an address you control. Read *Edge Functions > send-auth-email > Logs*: one `auth_email` line
   naming `resend`.
2. Set `MAIL_ORDER=mailjet,resend`, repeat, and see `mailjet`. Set a wrong Resend key and see a `resend` attempt marked
   `refused` followed by `mailjet` succeeding.
3. Tests of the logic (no network): `npm run test:unit` (`mail.test.mjs`).

## Honest limits
- Written against Supabase's documented hook payload and the Resend and Mailjet APIs; tested against **mock** providers only.
  `index.ts` (the Deno and webhook wrapper) has not been run: no Deno on the build machine.
- Two providers do not guarantee delivery: both need the sending domain's DNS (SPF, DKIM) set up, or mail lands in spam.
- A provider's free plan has daily and monthly limits: check them against the sign-ups you expect.
