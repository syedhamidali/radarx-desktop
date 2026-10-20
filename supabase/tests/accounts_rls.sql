-- Security checks for the accounts schema. Run against a LOCAL stack only:
--   psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f supabase/tests/accounts_rls.sql
-- Everything (including the two helper functions) happens in one transaction that is rolled back,
-- so nothing is left behind.
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
  insert into public.t_log values (what);   -- also readable before the rollback (see the harness)
  raise notice 'ok   %', what;
end $$;

do $t$
declare
  a uuid := gen_random_uuid();
  b uuid := gen_random_uuid();
  n int;
  failed boolean;
  r jsonb;
begin
  -- sign-up: the trigger builds the profile from the metadata
  insert into auth.users (id, email, raw_user_meta_data) values
    (a, 'a@example.test', '{"full_name":"Ada Radar","affiliation":"NCAR","use_case":"research","terms_version":"2026-10"}'),
    (b, 'bob@example.test', '{"use_case":"not-a-real-use"}');
  perform public.t_expect((select display_name from public.profiles where id = a) = 'Ada Radar', 'profile name comes from sign-up');
  perform public.t_expect((select affiliation from public.profiles where id = a) = 'NCAR', 'affiliation comes from sign-up');
  perform public.t_expect((select terms_accepted_at is not null from public.profiles where id = a), 'terms acceptance is recorded');
  perform public.t_expect((select display_name from public.profiles where id = b) = 'bob', 'name falls back to the e-mail');
  perform public.t_expect((select use_case is null from public.profiles where id = b), 'an invalid use case is dropped, sign-up still works');

  -- A sees and edits only A
  perform public.t_as_user(a);
  perform public.t_expect((select count(*) from public.profiles) = 1, 'A reads exactly one profile (their own)');
  update public.profiles set affiliation = 'UCAR' where id = a;
  perform public.t_expect((select affiliation from public.profiles where id = a) = 'UCAR', 'A can edit their profile');
  update public.profiles set display_name = 'hacked' where id = b;
  get diagnostics n = row_count;
  perform public.t_expect(n = 0, 'A cannot edit B''s profile');
  failed := false;
  begin insert into public.profiles (id) values (gen_random_uuid()); exception when others then failed := true; end;
  perform public.t_expect(failed, 'a client cannot insert profiles');

  -- synced items
  insert into public.synced_items (kind, name, data) values ('recipe', 'my recipe', '{"x":1}');
  perform public.t_expect((select count(*) from public.synced_items) = 1, 'A can store an item (user_id defaults to A)');
  failed := false;
  begin insert into public.synced_items (user_id, kind, name, data) values (b, 'recipe', 'planted', '{}'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'A cannot store an item as B');
  failed := false;
  begin insert into public.synced_items (kind, name, data) values ('recipe', 'huge', jsonb_build_object('x', repeat('a', 300000))); exception when others then failed := true; end;
  perform public.t_expect(failed, 'an item over 256 KB is refused');
  failed := false;
  begin insert into public.synced_items (kind, name, data) values ('nonsense', 'x', '{}'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'an unknown item kind is refused');

  -- devices
  r := public.register_device('device-aaaa-1111', 'Mac', 'MacIntel', '0.3.0');
  perform public.t_expect((r ->> 'count')::int = 1, 'register_device records the computer');
  r := public.register_device('device-aaaa-1111', 'Mac', 'MacIntel', '0.3.1');
  perform public.t_expect((r ->> 'count')::int = 1, 'registering again does not duplicate');
  failed := false;
  begin insert into public.devices (user_id, device_id) values (a, 'direct-insert-1'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'devices cannot be inserted directly');

  -- B sees nothing of A
  reset role;
  perform public.t_as_user(b);
  perform public.t_expect((select count(*) from public.synced_items) = 0, 'B sees none of A''s items');
  perform public.t_expect((select count(*) from public.devices) = 0, 'B sees none of A''s devices');
  perform public.t_expect((select count(*) from public.profiles) = 1, 'B sees only their own profile');
  delete from public.devices;
  reset role;
  perform public.t_expect((select count(*) from public.devices where user_id = a) = 1, 'B cannot delete A''s devices');

  -- the not-signed-in role gets nothing
  execute 'set local role anon';
  perform public.t_expect((select count(*) from public.profiles) = 0, 'a signed-out visitor sees no profiles');
  perform public.t_expect((select count(*) from public.synced_items) = 0, 'a signed-out visitor sees no items');
  failed := false;
  begin insert into public.synced_items (user_id, kind, name, data) values (gen_random_uuid(), 'recipe', 'x', '{}'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'a signed-out visitor cannot write items');
  failed := false;
  begin perform public.register_device('device-anon-0001'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'anon cannot register a device');
  reset role;

  -- deleting an account removes everything of theirs, and only theirs
  perform public.t_as_user(a);
  perform public.delete_my_account();
  reset role;
  perform public.t_expect((select count(*) from auth.users where id = a) = 0, 'delete_my_account removes the user');
  perform public.t_expect((select count(*) from public.profiles where id = a) = 0, '... and the profile');
  perform public.t_expect((select count(*) from public.synced_items where user_id = a) = 0, '... and the synced items');
  perform public.t_expect((select count(*) from public.devices where user_id = a) = 0, '... and the devices');
  perform public.t_expect((select count(*) from public.profiles where id = b) = 1, 'B is untouched');

  -- the item quota
  insert into public.synced_items (user_id, kind, name, data)
    select b, 'recipe', 'r' || g, '{}' from generate_series(1, 500) g;
  failed := false;
  begin insert into public.synced_items (user_id, kind, name, data) values (b, 'recipe', 'one too many', '{}'); exception when others then failed := true; end;
  perform public.t_expect(failed, 'the 501st synced item is refused');
end
$t$;

rollback;
\echo ALL ACCOUNT CHECKS PASSED
