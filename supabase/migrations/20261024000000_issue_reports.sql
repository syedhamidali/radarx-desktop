-- radarx issue reports: "report an issue" from the app and from the public website (docs/REPORTING.md).
--
-- Design rules
--   * Only the Edge Function `report-issue` (service role) writes here. It validates, sanitises, checks
--     the sign-in (app) or Turnstile (web) and the rate limits first. The constraints below are the second
--     line: a bug in the function still cannot store an oversized or malformed report.
--   * No client role (anon or authenticated) can insert, update or delete: there is no policy for it AND the
--     privileges are revoked. A signed-in user can READ their own reports (transparency, same as the activity
--     log); nobody else's.
--   * Deleting an account sets user_id to null: the report stays, no longer linked to anyone (the privacy
--     notice says so). A person can ask for a report to be deleted by e-mail.
--   * The visitor's IP address is never stored. The rate limiter keeps a salted SHA-256 of it (computed in the
--     function; the salt is a function secret) per hourly bucket in issue_report_limits, and the rows are
--     deleted after three days.
--   * Retention: purge_issue_reports(days) (default policy 24 months). Scheduling it (pg_cron or the
--     dashboard) is left to the owner; see docs/REPORTING.md.

create table public.issue_reports (
  id             uuid primary key default gen_random_uuid(),
  created_at     timestamptz not null default now(),
  -- null for web reports and after the account was deleted; never trusted from the request body
  user_id        uuid references auth.users (id) on delete set null,
  source         text not null check (source in ('app', 'web')),
  kind           text not null check (kind in ('bug', 'idea', 'question')),
  title          text not null check (char_length(title) between 3 and 140 and char_length(btrim(title)) >= 3),
  description    text not null check (char_length(description) between 10 and 5000),
  contact_email  text check (char_length(contact_email) <= 200 and contact_email ~ '^[^[:space:]@<>"'',;()\[\]\\]+@[^[:space:]@<>"'',;()\[\]\\]+\.[^[:space:]@<>"'',;()\[\]\\]{2,}$'),
  app_version    text check (char_length(app_version) <= 40),
  os             text check (char_length(os) <= 60),
  engine_version text check (char_length(engine_version) <= 40),
  diagnostics    text check (char_length(diagnostics) <= 20000),
  status         text not null default 'new' check (status in ('new', 'triaged', 'closed')),
  updated_at     timestamptz not null default now(),
  -- a web report has no account behind it (an app report loses its user_id when the account is deleted)
  check (source <> 'web' or user_id is null)
);

create index issue_reports_user_idx on public.issue_reports (user_id, created_at desc) where user_id is not null;
create index issue_reports_created_idx on public.issue_reports (created_at);
create index issue_reports_status_idx on public.issue_reports (status, created_at desc);

create trigger issue_reports_touch before update on public.issue_reports
  for each row execute function public.touch_updated_at();

alter table public.issue_reports enable row level security;

-- A signed-in user reads their own reports. Nothing else: no insert, update or delete policy exists.
create policy "issue_reports: read own" on public.issue_reports
  for select to authenticated using (user_id = (select auth.uid()));

-- Supabase grants new tables to the client roles by default; take it back explicitly (defence in depth).
revoke all on public.issue_reports from public, anon, authenticated;
grant select on public.issue_reports to authenticated;
grant all on public.issue_reports to service_role;

-- ---------------------------------------------------------------------------------------------
-- rate-limit buckets: (key, bucket) -> count. Keys are 'ip:<salted hash>', 'global:web', 'global:app'.

create table public.issue_report_limits (
  key    text        not null check (char_length(key) between 1 and 100),
  bucket timestamptz not null,
  count  int         not null default 0 check (count >= 0),
  primary key (key, bucket)
);
create index issue_report_limits_bucket_idx on public.issue_report_limits (bucket);

alter table public.issue_report_limits enable row level security;
-- no policy for any role: only the service role (which bypasses RLS) can touch it
revoke all on public.issue_report_limits from public, anon, authenticated;
grant all on public.issue_report_limits to service_role;

-- Counts one use of `p_key` in `p_bucket` unless the bucket is already at `p_max`. Returns true when the use was
-- counted (allowed), false when the limit is reached. Atomic: the conflict clause only updates a row below the
-- limit, so concurrent callers cannot both slip past it. Old buckets (more than three days) are removed here too,
-- so hashed addresses do not outlive their purpose even if the purge job is never scheduled.
create or replace function public.take_issue_limit(p_key text, p_bucket timestamptz, p_max int)
returns boolean
language plpgsql
set search_path = ''
as $$
declare
  got int;
begin
  if p_max < 1 then
    return false;
  end if;
  delete from public.issue_report_limits where bucket < now() - interval '3 days';
  insert into public.issue_report_limits as l (key, bucket, count)
  values (p_key, p_bucket, 1)
  on conflict (key, bucket) do update set count = l.count + 1 where l.count < p_max
  returning l.count into got;
  return got is not null;
end;
$$;

-- Stores one report. For a signed-in (app) report, at most `p_user_cap` per rolling 24 hours per account, counted
-- under a per-user advisory lock so concurrent submissions cannot both pass; over it, P0001 'report limit'.
-- With `p_global_key`, one use of that daily bucket is also taken (a safety net over all accounts), but only AFTER
-- the account's own cap passed, so one account hammering the endpoint cannot use up everybody's allowance;
-- when the bucket is full, P0001 'global limit'. One call is one transaction: a refusal stores and counts nothing.
create or replace function public.insert_issue_report(
  p_user_id uuid, p_source text, p_kind text, p_title text, p_description text,
  p_contact_email text default null, p_app_version text default null, p_os text default null,
  p_engine_version text default null, p_diagnostics text default null, p_user_cap int default 10,
  p_global_key text default null, p_global_max int default null
)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  new_id uuid;
  cnt int;
begin
  if p_user_id is not null then
    perform pg_advisory_xact_lock(hashtextextended('issue_reports:' || p_user_id::text, 0));
    select count(*) into cnt from public.issue_reports
     where user_id = p_user_id and created_at > now() - interval '24 hours';
    if cnt >= p_user_cap then
      raise exception 'report limit' using errcode = 'P0001';
    end if;
  end if;
  if p_global_key is not null then
    if p_global_max is null then
      raise exception 'p_global_max is required with p_global_key' using errcode = '22023';
    end if;
    if not public.take_issue_limit(p_global_key, date_trunc('day', now() at time zone 'utc') at time zone 'utc', p_global_max) then
      raise exception 'global limit' using errcode = 'P0001';
    end if;
  end if;
  insert into public.issue_reports
    (user_id, source, kind, title, description, contact_email, app_version, os, engine_version, diagnostics)
  values
    (p_user_id, p_source, p_kind, p_title, p_description, p_contact_email, p_app_version, p_os, p_engine_version, p_diagnostics)
  returning id into new_id;
  return new_id;
end;
$$;

-- Retention: deletes reports older than `days` (policy: 24 months = 730) and the expired rate-limit buckets.
-- Returns the number of reports deleted. Service role only. Schedule it, for example with pg_cron:
--   select cron.schedule('purge-issue-reports', '17 3 * * 0', $$select public.purge_issue_reports(730)$$);
create or replace function public.purge_issue_reports(days int default 730)
returns int
language plpgsql
set search_path = ''
as $$
declare
  n int;
begin
  if days is null or days < 1 then
    raise exception 'days must be at least 1' using errcode = '22023';
  end if;
  delete from public.issue_reports where created_at < now() - make_interval(days => days);
  get diagnostics n = row_count;
  delete from public.issue_report_limits where bucket < now() - interval '3 days';
  return n;
end;
$$;

-- The owner's triage list (Studio, or any service-role query): newest first, with a short excerpt and whether
-- diagnostics came along. Not readable by any client role.
create or replace view public.issue_reports_triage
  with (security_invoker = true) as
select id, created_at, status, source, kind, title,
       left(description, 300) as excerpt,
       contact_email, app_version, os, engine_version,
       (diagnostics is not null) as has_diagnostics,
       (user_id is not null) as has_account
  from public.issue_reports
 order by created_at desc;

revoke all on public.issue_reports_triage from public, anon, authenticated;
grant select on public.issue_reports_triage to service_role;

revoke all on function public.take_issue_limit(text, timestamptz, int) from public, anon, authenticated;
grant execute on function public.take_issue_limit(text, timestamptz, int) to service_role;
revoke all on function public.insert_issue_report(uuid, text, text, text, text, text, text, text, text, text, int, text, int)
  from public, anon, authenticated;
grant execute on function public.insert_issue_report(uuid, text, text, text, text, text, text, text, text, text, int, text, int)
  to service_role;
revoke all on function public.purge_issue_reports(int) from public, anon, authenticated;
grant execute on function public.purge_issue_reports(int) to service_role;
