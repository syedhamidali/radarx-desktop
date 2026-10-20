-- Security checks for the activity log (supabase/migrations/20261021000000_activity.sql).
-- Run after the accounts migration, against a LOCAL stack only:
--   psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f supabase/tests/activity_rls.sql
-- One transaction, rolled back at the end.
begin;

create or replace function public.t_as_user(uid uuid) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims', json_build_object('sub', uid, 'role', 'authenticated')::text, true);
  perform set_config('request.jwt.claim.sub', uid::text, true);
  execute 'set local role authenticated';
end $$;

create table public.t_log (msg text);
create or replace function public.t_expect(ok boolean, what text) returns void language plpgsql security definer as $$
begin
  if not ok then raise exception 'FAILED: %', what; end if;
  insert into public.t_log values (what);
  raise notice 'ok   %', what;
end $$;

-- runs a statement as the current role and says whether it was refused
create or replace function public.t_refused(stmt text) returns boolean language plpgsql as $$
begin
  execute stmt;
  return false;
exception when others then
  return true;
end $$;

do $t$
declare
  a uuid := gen_random_uuid();
  b uuid := gen_random_uuid();
  n int;
  failed boolean;
  dev text := 'abcdef1234567890';
begin
  insert into auth.users (id, email) values (a, 'a@example.test'), (b, 'b@example.test');

  -- A records their own activity
  perform public.t_as_user(a);
  insert into public.activity_events (device_id, surface, event, app_version, os, detail)
    values (dev, 'app', 'app_start', '0.3.0', 'macOS', '{}');
  insert into public.activity_events (device_id, surface, event, detail) values (dev, 'cli', 'command', '{"command":"info"}');
  insert into public.activity_events (device_id, surface, event, detail) values (dev, 'api', 'file_opened', '{"format":"NEXRAD"}');
  insert into public.activity_events (device_id, surface, event, detail) values (dev, 'app', 'module_run', '{"module":"radarx_hid"}');
  insert into public.activity_events (device_id, surface, event, detail) values (dev, 'app', 'export', '{"format":"cfradial2"}');
  insert into public.activity_events (device_id, surface, event, detail) values (dev, 'engine', 'error', '{"class":"XrdInputError"}');
  perform public.t_expect((select count(*) from public.activity_events) = 6, 'A can insert and read their own events (user_id defaults to A)');
  perform public.t_expect((select count(distinct user_id) from public.activity_events where user_id = a) = 1, '... and the rows carry A''s id');

  -- bulk insert (what the app sends) works
  insert into public.activity_events (device_id, surface, event, detail)
    select dev, 'app', 'qpe_run', '{"type":"rain rate"}' from generate_series(1, 3);
  perform public.t_expect((select count(*) from public.activity_events) = 9, 'a batch insert works');

  -- cannot write as another user
  perform public.t_expect(public.t_refused(format($f$insert into public.activity_events (user_id, device_id, surface, event) values (%L, %L, 'app', 'app_start')$f$, b, dev)),
    'A cannot insert an event as B');

  -- cannot update or delete, not even their own
  perform public.t_expect(public.t_refused('update public.activity_events set event = ''sign_in'''), 'A cannot update events');
  perform public.t_expect((select count(*) from public.activity_events where event = 'sign_in') = 0, '... the rows are unchanged');
  perform public.t_expect(public.t_refused('delete from public.activity_events'), 'A cannot delete events');
  perform public.t_expect((select count(*) from public.activity_events) = 9, '... the rows are still there');

  -- allow-list: event names
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event) values ('abcdef1234567890', 'app', 'opened_secret_feature')$s$),
    'an event name outside the allow-list is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event) values ('abcdef1234567890', 'phone', 'app_start')$s$),
    'an unknown surface is refused');
  -- allow-list: detail keys
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'cli', 'command', '{"command":"info","file":"x"}')$s$),
    'a detail key outside the allow-list is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'cli', 'command', '{"path":"info"}')$s$),
    'a path key is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'cli', 'app_start', '{"command":"info"}')$s$),
    'a key that belongs to another event is refused');
  -- allow-list: values must be plain words (no path, URL, e-mail, coordinates with a dot)
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '{"format":"/Users/ada/KTLX20130520_201643_V06.gz"}')$s$),
    'a file path as a value is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '{"format":"C:\\data\\KTLX"}')$s$),
    'a Windows path as a value is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '{"format":"KTLX20130520_201643_V06.gz"}')$s$),
    'a file name (with a dot) as a value is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '{"format":"35.47,-97.52"}')$s$),
    'coordinates as a value are refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '{"format":["NEXRAD"]}')$s$),
    'a non-string value is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', '"NEXRAD"')$s$),
    'a detail that is not an object is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.activity_events (device_id, surface, event, detail) values ('abcdef1234567890', 'api', 'file_opened', jsonb_build_object('format', %L))$f$, repeat('a', 65))),
    'a value longer than 64 characters is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event) values ('my laptop name', 'app', 'app_start')$s$),
    'a device name (not a random token) is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event, os) values ('abcdef1234567890', 'app', 'app_start', '/Users/ada')$s$),
    'a path in the os column is refused');

  -- timestamps are clamped, created_at cannot be chosen by the client
  insert into public.activity_events (device_id, surface, event, occurred_at, created_at)
    values (dev, 'app', 'sign_in', now() - interval '400 days', now() - interval '400 days');
  perform public.t_expect((select occurred_at > now() - interval '1 day' and created_at > now() - interval '1 day'
                             from public.activity_events where event = 'sign_in' and user_id = a), 'out-of-range times are replaced by the server time');

  -- B sees nothing of A, A nothing of B
  reset role;
  perform public.t_as_user(b);
  perform public.t_expect((select count(*) from public.activity_events) = 0, 'B cannot read A''s events');
  insert into public.activity_events (device_id, surface, event) values ('bbbbbbbb11111111', 'cli', 'app_start');
  perform public.t_expect((select count(*) from public.activity_events) = 1, 'B reads only their own');
  perform public.t_expect(public.t_refused('delete from public.activity_events'), 'B cannot delete anything');
  reset role;
  perform public.t_expect((select count(*) from public.activity_events where user_id = a) = 10, 'B cannot delete A''s events');

  -- anon sees and writes nothing
  execute 'set local role anon';
  perform public.t_expect(public.t_refused('select count(*) from public.activity_events'), 'anon cannot read events (no privilege)');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (user_id, device_id, surface, event) values (gen_random_uuid(), 'abcdef1234567890', 'app', 'app_start')$s$),
    'anon cannot write events');
  reset role;

  -- the owner's view and the purge function are not for users
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused('select * from public.activity_overview'), 'a signed-in user cannot read the owner view');
  perform public.t_expect(public.t_refused('select public.purge_old_activity(1)'), 'a signed-in user cannot run the purge');
  reset role;
  execute 'set local role anon';
  perform public.t_expect(public.t_refused('select * from public.activity_overview'), 'anon cannot read the owner view');
  perform public.t_expect(public.t_refused('select public.purge_old_activity(1)'), 'anon cannot run the purge');
  reset role;
  execute 'set local role service_role';
  perform public.t_expect((select count(*) from public.activity_overview) = 2, 'the service role reads the owner view (one row per user)');
  perform public.t_expect((select events_total from public.activity_overview where user_id = a) = 10, '... with A''s event count');
  reset role;

  -- retention: purge removes old rows only
  update public.activity_events set occurred_at = now() - interval '400 days' where user_id = a and event = 'export';
  execute 'set local role service_role';
  perform public.t_expect(public.purge_old_activity(365) = 1, 'purge_old_activity(365) removes the one row older than a year');
  perform public.t_expect((select count(*) from public.activity_events where user_id = a) = 9, '... and keeps the rest');
  failed := public.t_refused('select public.purge_old_activity(0)');
  perform public.t_expect(failed, 'purge_old_activity(0) is refused');
  reset role;

  -- the daily cap: 2000 per user per 24 hours
  perform public.t_as_user(b);
  insert into public.activity_events (device_id, surface, event)
    select 'bbbbbbbb11111111', 'cli', 'app_start' from generate_series(1, 1998);
  perform public.t_expect((select count(*) from public.activity_events) = 1999, 'B can insert up to the daily cap');
  insert into public.activity_events (device_id, surface, event) values ('bbbbbbbb11111111', 'cli', 'app_start');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event) values ('bbbbbbbb11111111', 'cli', 'app_start')$s$),
    'the 2001st event within 24 hours is refused');
  perform public.t_expect(public.t_refused($s$insert into public.activity_events (device_id, surface, event) select 'bbbbbbbb11111111', 'cli', 'app_start' from generate_series(1, 5)$s$),
    'a batch over the cap is refused as a whole');
  perform public.t_expect((select count(*) from public.activity_events) = 2000, '... and nothing of it was stored');
  reset role;

  -- a user insert for A is unaffected by B's cap
  perform public.t_as_user(a);
  insert into public.activity_events (device_id, surface, event) values (dev, 'cli', 'app_start');
  perform public.t_expect(true, 'the cap is per user');
  reset role;

  -- deleting the account removes its activity
  perform public.t_as_user(a);
  perform public.delete_my_account();
  reset role;
  perform public.t_expect((select count(*) from public.activity_events where user_id = a) = 0, 'delete_my_account removes the activity');
  perform public.t_expect((select count(*) from public.activity_events where user_id = b) = 2000, '... and only that user''s');
end
$t$;

rollback;
\echo ALL ACTIVITY CHECKS PASSED
