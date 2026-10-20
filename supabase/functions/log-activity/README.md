# log-activity

The activity log of a session that uses a personal access token. See `../exchange-api-token/README.md` for the set-up
(both functions are deployed together) and `docs/ACCOUNTS.md` for the design.

`allowlist.json` is the one copy of the allow-list this function applies. It must equal the lists in
`python/engine/account/activity.py` and in `supabase/migrations/20261021000000_activity.sql`:
`python/tests/test_account_activity_allowlist.py` fails when they drift. The table's trigger still runs on the insert,
so its daily cap (2000 events per account) applies here as well.
