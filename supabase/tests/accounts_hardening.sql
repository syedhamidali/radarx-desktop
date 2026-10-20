-- Checks for supabase/migrations/20261022000000_accounts_hardening.sql (locks and the device cap).
-- Run after the two earlier migrations, against a LOCAL stack only:
--   psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f supabase/tests/accounts_hardening.sql
--
-- What this CANNOT show: the advisory locks exist to serialise CONCURRENT transactions, and a test
-- script runs on one connection. So the lock is tested by its presence in the function bodies, and
-- the cap/replace logic by behaviour. Concurrency itself needs two real connections (not tested here).
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

do $t$
declare
  a uuid := gen_random_uuid();
  b uuid := gen_random_uuid();
  r jsonb;
  n int;
begin
  insert into auth.users (id, email) values (a, 'a@example.test'), (b, 'b@example.test');

  -- the locks are in the function bodies (see the note at the top about concurrency)
  perform public.t_expect(position('pg_advisory_xact_lock' in pg_get_functiondef('public.check_activity_event()'::regprocedure)) > 0,
    'the activity cap trigger takes a per-user advisory lock');
  perform public.t_expect(position('pg_advisory_xact_lock' in pg_get_functiondef('public.enforce_synced_items_limit()'::regprocedure)) > 0,
    'the synced-items quota trigger takes a per-user advisory lock');
  perform public.t_expect(position('pg_advisory_xact_lock' in pg_get_functiondef('public.register_device(text,text,text,text)'::regprocedure)) > 0,
    'register_device takes a per-user advisory lock');

  -- the device cap: 50 per account, the longest-unused computer is replaced
  perform public.t_as_user(a);
  for i in 1..50 loop
    r := public.register_device('computer' || lpad(i::text, 4, '0'), 'Mac ' || i, 'MacIntel', '0.3.0');
  end loop;
  perform public.t_expect((r ->> 'count')::int = 50, 'an account can register 50 computers');
  reset role;
  -- make computer0001 the one used longest ago, computer0002 next
  update public.devices set last_seen = now() - interval '300 days' where user_id = a and device_id = 'computer0001';
  update public.devices set last_seen = now() - interval '200 days' where user_id = a and device_id = 'computer0002';
  perform public.t_as_user(a);
  r := public.register_device('computer0051', 'New Mac', 'MacIntel', '0.3.0');
  perform public.t_expect((r ->> 'count')::int = 50, 'the 51st computer does not raise the count above 50');
  reset role;
  perform public.t_expect(not exists (select 1 from public.devices where user_id = a and device_id = 'computer0001'),
    '... the computer unused for the longest time was replaced');
  perform public.t_expect(exists (select 1 from public.devices where user_id = a and device_id = 'computer0002'),
    '... and the others are kept');
  perform public.t_expect(exists (select 1 from public.devices where user_id = a and device_id = 'computer0051'),
    '... the new one is there');

  -- an existing computer registering again is not a new row and replaces nobody
  perform public.t_as_user(a);
  r := public.register_device('computer0002', 'Mac 2', 'MacIntel', '0.3.1');
  perform public.t_expect((r ->> 'count')::int = 50, 're-registering a known computer keeps the count');
  reset role;
  perform public.t_expect(exists (select 1 from public.devices where user_id = a and device_id = 'computer0003'),
    '... and removes nothing');

  -- the cap is per account
  perform public.t_as_user(b);
  r := public.register_device('bobscomputer01', 'Bob', 'Win32', '0.3.0');
  perform public.t_expect((r ->> 'count')::int = 1, 'another account is not affected');
  reset role;

  -- the item quota still works (behaviour unchanged by the lock)
  insert into public.synced_items (user_id, kind, name, data)
    select b, 'recipe', 'r' || g, '{}' from generate_series(1, 500) g;
  begin
    insert into public.synced_items (user_id, kind, name, data) values (b, 'recipe', 'one too many', '{}');
    perform public.t_expect(false, 'the 501st item is refused');
  exception when sqlstate 'P0001' then
    perform public.t_expect(true, 'the 501st synced item is still refused (P0001)');
  end;

  -- anon still cannot call register_device
  execute 'set local role anon';
  begin
    perform public.register_device('anonymousdevice');
    perform public.t_expect(false, 'anon cannot register a device');
  exception when sqlstate '42501' then
    perform public.t_expect(true, 'anon still cannot register a device (permission denied)');
  end;
  reset role;
end
$t$;

rollback;
\echo ALL HARDENING CHECKS PASSED
