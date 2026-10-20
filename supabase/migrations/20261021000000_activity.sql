-- radarx activity log: which FEATURES an account uses (docs/PRIVACY.md "Activity").
--
-- Design rules
--   * Feature level only. The allow-lists below are enforced HERE, in the database, so a client bug
--     or a modified client cannot store more: event names, which detail keys each event may carry,
--     and what a value may look like (letters, digits, space, _ + - only: no dot, slash, colon or
--     backslash, so no path, URL or e-mail address fits).
--   * NEVER stored: file names or paths, coordinates or place names, radar site identifiers, file
--     contents, free text typed by the user. There is no column or key that could hold them.
--   * A user can INSERT and SELECT their own rows (transparency: the app lists them). Nobody but the
--     service role can update or delete, and anon can do nothing.
--   * Limits: at most 2000 inserts per user per 24 hours (counted under a per-user advisory lock so
--     concurrent inserts cannot slip past it), 200 bytes of detail, clamped timestamps; app_version
--     must look like a version and os is one of five fixed words.
--   * Retention: purge_old_activity(days). Scheduling it (pg_cron or the dashboard) is left to the
--     owner; see docs/ACCOUNTS.md.
--   * Deleting an account removes its rows (on delete cascade).

create table public.activity_events (
  id          bigint generated always as identity primary key,
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  device_id   text not null check (char_length(device_id) between 8 and 80),
  occurred_at timestamptz not null default now(),
  surface     text not null check (surface in ('app', 'cli', 'api', 'engine')),
  event       text not null check (char_length(event) <= 40),
  app_version text check (char_length(app_version) <= 40),
  os          text check (char_length(os) <= 40),
  detail      jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create index activity_events_user_time_idx on public.activity_events (user_id, occurred_at desc);
create index activity_events_created_idx on public.activity_events (created_at);

-- ---------------------------------------------------------------------------------------------
-- the allow-list, checked on every insert

create or replace function public.check_activity_event()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  allowed_keys text[];
  k text;
  v jsonb;
  daily_cap constant int := 2000;
  cnt int;
begin
  -- One insert at a time per user: without this, two concurrent transactions both count the same rows
  -- and both pass the daily cap. (Released at the end of the transaction.)
  perform pg_advisory_xact_lock(hashtextextended('activity:' || coalesce(new.user_id::text, ''), 0));

  -- event name -> the detail keys it may carry
  allowed_keys := case new.event
    when 'app_start'   then array[]::text[]
    when 'sign_in'     then array[]::text[]
    when 'sign_out'    then array[]::text[]
    when 'command'     then array['command']
    when 'module_run'  then array['module']
    when 'file_opened' then array['format']
    when 'export'      then array['format']
    when 'mosaic_run'  then array['type']
    when 'winds_run'   then array['type']
    when 'animate_run' then array['type']
    when 'qpe_run'     then array['type']
    when 'error'       then array['class']
    else null
  end;
  if allowed_keys is null then
    raise exception 'activity event "%" is not allowed', left(new.event, 40) using errcode = '23514';
  end if;

  if jsonb_typeof(new.detail) is distinct from 'object' then
    raise exception 'activity detail must be an object' using errcode = '23514';
  end if;
  if octet_length(new.detail::text) > 200 then
    raise exception 'activity detail is too large' using errcode = '23514';
  end if;
  for k, v in select * from jsonb_each(new.detail) loop
    if not (k = any (allowed_keys)) then
      raise exception 'detail key "%" is not allowed for event "%"', left(k, 40), new.event using errcode = '23514';
    end if;
    if jsonb_typeof(v) is distinct from 'string' or (v #>> '{}') !~ '^[A-Za-z0-9_ +-]{1,64}$' then
      raise exception 'detail value for "%" is not a plain short word', k using errcode = '23514';
    end if;
  end loop;

  -- a device id is a random token, never a name
  if new.device_id !~ '^[A-Za-z0-9]{8,80}$' then
    raise exception 'device_id must be a random token' using errcode = '23514';
  end if;
  -- a version number ("0.3.0", "0.3.0-rc1") or "unknown": never free text
  if new.app_version is not null
     and new.app_version !~ '^[0-9]{1,4}(\.[0-9A-Za-z+_-]{1,12}){0,3}$' and new.app_version <> 'unknown' then
    raise exception 'app_version is not a version' using errcode = '23514';
  end if;
  if new.os is not null and new.os not in ('macOS', 'Windows', 'Linux', 'Web', 'Other') then
    raise exception 'os is not an operating-system family' using errcode = '23514';
  end if;

  -- the clock of the client is a hint only: out of range (60 days old, 5 minutes ahead) -> now
  if new.occurred_at < now() - interval '60 days' or new.occurred_at > now() + interval '5 minutes' then
    new.occurred_at := now();
  end if;

  -- the insert time is the server's, never the client's (the cap below counts it)
  new.created_at := now();

  -- per-user daily insert cap
  select count(*) into cnt
    from public.activity_events
   where user_id = new.user_id and created_at > now() - interval '24 hours';
  if cnt >= daily_cap then
    raise exception 'daily activity limit (%) reached', daily_cap using errcode = 'P0001';
  end if;
  return new;
end;
$$;

create trigger activity_events_check before insert on public.activity_events
  for each row execute function public.check_activity_event();

-- ---------------------------------------------------------------------------------------------
-- row-level security: own rows only, insert and select only

alter table public.activity_events enable row level security;

create policy "activity: add own" on public.activity_events
  for insert to authenticated with check (user_id = (select auth.uid()));
create policy "activity: read own" on public.activity_events
  for select to authenticated using (user_id = (select auth.uid()));
-- no update and no delete policy: rows can only be removed by the retention function, by deleting the
-- account, or by the service role.

-- belt and braces: Supabase grants every new table to anon / authenticated by default; take it back.
revoke all on public.activity_events from anon, authenticated;
grant insert, select on public.activity_events to authenticated;
revoke all on sequence public.activity_events_id_seq from anon, authenticated;
grant usage on sequence public.activity_events_id_seq to authenticated;

-- ---------------------------------------------------------------------------------------------
-- retention: the owner schedules this (for example monthly) and chooses the number of days

create or replace function public.purge_old_activity(days int default 365)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  n bigint;
begin
  if days is null or days < 1 then
    raise exception 'days must be at least 1' using errcode = '22023';
  end if;
  delete from public.activity_events where occurred_at < now() - make_interval(days => days);
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.purge_old_activity(int) from public, anon, authenticated;
grant execute on function public.purge_old_activity(int) to service_role;

-- ---------------------------------------------------------------------------------------------
-- the owner's view: who uses radarx-desktop, how much, on which surface. Only the service role (the
-- owner's dashboard / SQL editor) can read it; it joins the e-mail, so it must never be exposed.

create or replace view public.activity_overview
with (security_invoker = false) as
select
  u.id                                                       as user_id,
  u.email                                                    as email,
  p.display_name                                             as display_name,
  p.affiliation                                              as affiliation,
  p.use_case                                                 as use_case,
  min(a.occurred_at)                                         as first_seen,
  max(a.occurred_at)                                         as last_seen,
  count(*)                                                   as events_total,
  count(*) filter (where a.occurred_at > now() - interval '30 days') as events_30d,
  count(*) filter (where a.surface = 'app')                  as app_events,
  count(*) filter (where a.surface = 'cli')                  as cli_events,
  count(*) filter (where a.surface = 'api')                  as api_events,
  count(distinct a.device_id)                                as devices,
  max(a.app_version)                                         as latest_version
from public.activity_events a
join auth.users u on u.id = a.user_id
left join public.profiles p on p.id = a.user_id
group by u.id, u.email, p.display_name, p.affiliation, p.use_case;

revoke all on public.activity_overview from public, anon, authenticated;
grant select on public.activity_overview to service_role;
