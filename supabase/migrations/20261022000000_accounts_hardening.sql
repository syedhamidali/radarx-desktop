-- Hardening of the accounts schema (after an independent review of 20261020000000_accounts.sql).
--
--   * The per-user quotas (500 synced items; devices below) count rows and then insert. Two concurrent
--     transactions can both count "499" and both insert. A per-user transaction-scoped advisory lock
--     makes the count-then-insert atomic.
--   * register_device() is callable by any signed-in user and used to create rows without limit: it now
--     keeps at most 50 computers per account. When a 51st registers, the computer that has not been
--     used for the longest time is replaced (an account that sets up many machines keeps the recent
--     ones; it is never refused).
-- Nothing else changes: same tables, same policies.

create or replace function public.enforce_synced_items_limit()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  lim constant int := 500;
  cnt int;
begin
  perform pg_advisory_xact_lock(hashtextextended('synced_items:' || new.user_id::text, 0));
  select count(*) into cnt from public.synced_items where user_id = new.user_id;
  if cnt >= lim then
    raise exception 'synced item limit (%) reached', lim using errcode = 'P0001';
  end if;
  return new;
end;
$$;

create or replace function public.register_device(
  p_device_id text, p_name text default null, p_platform text default null, p_app_version text default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := (select auth.uid());
  max_devices constant int := 50;
  cnt int;
begin
  if uid is null then
    raise exception 'not signed in' using errcode = '28000';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('devices:' || uid::text, 0));
  insert into public.devices (user_id, device_id, name, platform, app_version)
  values (uid, p_device_id, left(p_name, 120), left(p_platform, 40), left(p_app_version, 40))
  on conflict (user_id, device_id)
  do update set last_seen = now(),
                name = coalesce(excluded.name, public.devices.name),
                platform = coalesce(excluded.platform, public.devices.platform),
                app_version = coalesce(excluded.app_version, public.devices.app_version);
  select count(*) into cnt from public.devices where user_id = uid;
  if cnt > max_devices then
    -- replace the computers used longest ago (never the one that just registered)
    delete from public.devices
     where id in (select id from public.devices
                   where user_id = uid and device_id <> p_device_id
                   order by last_seen asc
                   limit cnt - max_devices);
    select count(*) into cnt from public.devices where user_id = uid;
  end if;
  return jsonb_build_object('registered', true, 'count', cnt);
end;
$$;

revoke all on function public.register_device(text, text, text, text) from public, anon;
grant execute on function public.register_device(text, text, text, text) to authenticated;
