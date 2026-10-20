-- Security checks for the personal access tokens (supabase/migrations/20261023000000_api_tokens.sql).
-- Run after the earlier migrations, against a LOCAL stack only:
--   psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f supabase/tests/api_tokens.sql
-- One transaction, rolled back at the end. The checks name the error they expect, so a typo cannot
-- pass as "refused".
begin;

create or replace function public.t_as_user(uid uuid, anonymous boolean default false) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims',
    json_build_object('sub', uid, 'role', 'authenticated', 'is_anonymous', anonymous)::text, true);
  perform set_config('request.jwt.claim.sub', uid::text, true);
  execute 'set local role authenticated';
end $$;

create or replace function public.t_as_service() returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims', json_build_object('role', 'service_role')::text, true);
  perform set_config('request.jwt.claim.sub', '', true);
  execute 'set local role service_role';
end $$;

create or replace function public.t_as_anon() returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims', json_build_object('role', 'anon')::text, true);
  perform set_config('request.jwt.claim.sub', '', true);
  execute 'set local role anon';
end $$;

create table public.t_log (msg text);
create or replace function public.t_expect(ok boolean, what text) returns void language plpgsql security definer as $$
begin
  if not ok then raise exception 'FAILED: %', what; end if;
  insert into public.t_log values (what);
  raise notice 'ok   %', what;
end $$;

create or replace function public.t_refused(stmt text, expect text) returns boolean language plpgsql as $$
declare st text; msg text;
begin
  execute stmt;
  return false;
exception when others then
  get stacked diagnostics st = returned_sqlstate, msg = message_text;
  if st = expect or position(expect in msg) > 0 then return true; end if;
  raise exception 'WRONG ERROR for [%]: got % (%), expected %', left(stmt, 90), st, msg, expect;
end $$;

do $t$
declare
  a uuid := gen_random_uuid();
  b uuid := gen_random_uuid();
  r jsonb;
  tok text;
  tok_b text;
  id_a uuid;
  n int;
  seen text[] := array[]::text[];
  used timestamptz;
  used2 timestamptz;
  ok boolean;
begin
  insert into auth.users (id, email) values (a, 'a@example.test'), (b, 'b@example.test');

  -- ---------------------------------------------------------------- creation
  perform public.t_as_user(a);
  r := public.create_api_token('Slurm cluster', 90);
  tok := r ->> 'token';
  id_a := (r ->> 'id')::uuid;
  perform public.t_expect(tok ~ '^rxd_pat_[A-Za-z0-9_-]{43}$', 'a token is rxd_pat_ + 43 URL-safe characters');
  perform public.t_expect(char_length(tok) = 51, '... 51 characters in all (256 random bits)');
  perform public.t_expect((r ->> 'name') = 'Slurm cluster', 'the answer names the token');
  perform public.t_expect(abs(extract(epoch from (r ->> 'expires_at')::timestamptz - now()) - 90 * 86400) < 5,
    'the expiry is 90 days away');
  r := public.create_api_token('default days');  -- (not inside BETWEEN: it would run twice)
  perform public.t_expect((r ->> 'expires_at')::timestamptz
    between now() + interval '89 days 23 hours' and now() + interval '90 days 1 hour', 'the default is 90 days');
  perform public.t_expect(
    (public.create_api_token('year', 365) ->> 'expires_at')::timestamptz <= now() + interval '365 days 1 minute',
    'the maximum is 365 days');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', 366)$s$, '22023'), '366 days is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', 0)$s$, '22023'), '0 days is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', null)$s$, '22023'), 'null days is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('', 30)$s$, '22023'), 'an empty name is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('   ', 30)$s$, '22023'), 'a blank name is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token(null, 30)$s$, '22023'), 'a null name is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token(repeat('n', 65), 30)$s$, '22023'), 'a 65-character name is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('<script>alert(1)</script>', 30)$s$, '22023'),
    'markup in a name is refused');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('a/b', 30)$s$, '22023'), 'a slash in a name is refused');
  perform public.create_api_token(repeat('n', 64), 30);
  perform public.t_expect(true, 'a 64-character name is fine');
  reset role;

  -- ---------------------------------------------------------------- one-time display
  perform public.t_expect((select count(*) from public.api_tokens where token_hash = sha256(convert_to(tok, 'UTF8'))) = 1,
    'the database holds the SHA-256 of the token');
  perform public.t_expect((select count(*) from public.api_tokens t where t::text like '%' || substr(tok, 9) || '%') = 0,
    'the plaintext is stored nowhere in the row');
  perform public.t_expect((select count(*) from public.api_tokens t where t::text like '%' || tok || '%') = 0,
    '... not even with the prefix');
  perform public.t_expect((select hint from public.api_tokens where id = id_a) = substr(tok, 9, 4),
    'only the first four characters are kept as a hint');

  -- ---------------------------------------------------------------- the owner can list, never see the hash
  perform public.t_as_user(a);
  perform public.t_expect((select count(*) from public.my_api_tokens) = 4, 'A lists their own tokens through the view');
  perform public.t_expect((select count(*) from public.my_api_tokens where status = 'active') = 4, '... all active');
  perform public.t_expect((select name from public.my_api_tokens where id = id_a) = 'Slurm cluster', '... with name');
  perform public.t_expect(public.t_refused($s$select token_hash from public.api_tokens$s$, '42501'), 'A cannot select the hash');
  perform public.t_expect(public.t_refused($s$select * from public.api_tokens$s$, '42501'), 'select * is refused too (it includes the hash)');
  perform public.t_expect(public.t_refused($s$select t.token_hash from public.api_tokens t where true$s$, '42501'),
    'A cannot read the hash through an alias');
  perform public.t_expect(public.t_refused($s$select api_tokens::text from public.api_tokens$s$, '42501'),
    'A cannot read the whole row as text');
  perform public.t_expect(public.t_refused($s$select * from public.my_api_tokens where token_hash is not null$s$, '42703'),
    'the view has no hash column');
  perform public.t_expect((select count(*) from public.api_tokens) = 4, 'A may read the other columns of their own rows');
  perform public.t_expect(public.t_refused($s$select user_id, token_hash from public.api_tokens$s$, '42501'), 'mixed columns including the hash are refused');
  reset role;

  -- ---------------------------------------------------------------- no direct writes by clients
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused(
    format($s$insert into public.api_tokens (user_id, name, hint, token_hash, expires_at) values (%L, 'x', 'abcd', sha256('x'), now() + interval '1 day')$s$, a), '42501'),
    'A cannot insert a token directly');
  perform public.t_expect(public.t_refused($s$update public.api_tokens set revoked_at = null$s$, '42501'), 'A cannot update directly');
  perform public.t_expect(public.t_refused($s$update public.api_tokens set expires_at = now() + interval '3000 days'$s$, '42501'),
    'A cannot extend an expiry directly');
  perform public.t_expect(public.t_refused($s$delete from public.api_tokens$s$, '42501'), 'A cannot delete directly');
  perform public.t_expect(public.t_refused($s$select * from public.api_token_rate$s$, '42501'), 'A cannot read the rate table');
  reset role;

  -- ---------------------------------------------------------------- others see nothing
  perform public.t_as_user(b);
  perform public.t_expect((select count(*) from public.my_api_tokens) = 0, 'B sees none of A''s tokens in the view');
  perform public.t_expect((select count(*) from public.api_tokens) = 0, '... nor in the table');
  perform public.t_expect(public.revoke_api_token(id_a) = false, 'B cannot revoke A''s token');
  tok_b := public.create_api_token('b token', 30) ->> 'token';
  perform public.t_expect((select count(*) from public.my_api_tokens) = 1, 'B sees their own');
  reset role;
  perform public.t_expect((select revoked_at from public.api_tokens where id = id_a) is null, '... and A''s token is untouched');

  -- ---------------------------------------------------------------- anonymous callers
  perform public.t_as_anon();
  perform public.t_expect(public.t_refused($s$select * from public.my_api_tokens$s$, '42501'), 'anon cannot read the view');
  perform public.t_expect(public.t_refused($s$select id from public.api_tokens$s$, '42501'), 'anon cannot read the table');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', 1)$s$, '42501'), 'anon cannot create');
  perform public.t_expect(public.t_refused($s$select public.revoke_api_token(gen_random_uuid())$s$, '42501'), 'anon cannot revoke');
  perform public.t_expect(public.t_refused($s$select public.use_api_token(sha256('x'))$s$, '42501'), 'anon cannot look a token up');
  reset role;
  -- an "anonymous sign-in" is a Supabase user with role authenticated and is_anonymous true
  perform public.t_as_user(a, true);
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', 1)$s$, '28000'), 'an anonymous session cannot create a token');
  reset role;
  perform public.t_as_service();
  perform public.t_expect(public.t_refused($s$select public.create_api_token('x', 1)$s$, '28000'), 'the service role cannot create one (no signed-in user)');
  reset role;

  -- ---------------------------------------------------------------- lookup by hash: service role only
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused(format($s$select public.use_api_token(sha256(convert_to(%L, 'UTF8')))$s$, tok), '42501'),
    'a signed-in user cannot call the lookup');
  perform public.t_expect(public.t_refused($s$select public.api_token_rate_hit('k', 5, 60)$s$, '42501'), '... nor the rate counter');
  perform public.t_expect(public.t_refused($s$select public.purge_api_tokens(400)$s$, '42501'), '... nor the purge');
  reset role;
  perform public.t_as_service();
  r := public.use_api_token(sha256(convert_to(tok, 'UTF8')));
  perform public.t_expect((r ->> 'user_id')::uuid = a and (r ->> 'name') = 'Slurm cluster' and (r ->> 'email') = 'a@example.test',
    'the service role finds the token and its account');
  perform public.t_expect((r ->> 'id')::uuid = id_a and (r ->> 'expires_at') is not null, '... with id and expiry');
  perform public.t_expect(not (r ? 'token_hash') and not (r ? 'token'), '... and no hash or token in the answer');
  perform public.t_expect(public.use_api_token(sha256(convert_to('rxd_pat_' || repeat('A', 43), 'UTF8'))) is null, 'an unknown token gives null');
  perform public.t_expect(public.use_api_token(null) is null, 'null gives null');
  perform public.t_expect(public.use_api_token('\x00'::bytea) is null, 'a hash of the wrong length gives null');
  reset role;
  select last_used_at into used from public.api_tokens where id = id_a;
  perform public.t_expect(used is not null and used > now() - interval '1 minute', 'last_used_at is set on use');
  perform public.t_as_service();
  perform public.use_api_token(sha256(convert_to(tok, 'UTF8')));
  reset role;
  select last_used_at into used2 from public.api_tokens where id = id_a;
  perform public.t_expect(used2 = used, 'a second use within a minute writes nothing');
  update public.api_tokens set last_used_at = now() - interval '2 hours' where id = id_a;
  perform public.t_as_service();
  perform public.use_api_token(sha256(convert_to(tok, 'UTF8')));
  reset role;
  perform public.t_expect((select last_used_at from public.api_tokens where id = id_a) > now() - interval '1 minute', 'a later use updates it');

  -- ---------------------------------------------------------------- expiry, ban, deletion
  update public.api_tokens set created_at = now() - interval '10 days', expires_at = now() - interval '1 day' where id = id_a;
  perform public.t_as_service();
  perform public.t_expect(public.use_api_token(sha256(convert_to(tok, 'UTF8'))) is null, 'an expired token is refused');
  reset role;
  perform public.t_as_user(a);
  perform public.t_expect((select status from public.my_api_tokens where id = id_a) = 'expired', 'the list says expired');
  reset role;
  perform public.t_as_service();
  perform public.t_expect(public.use_api_token(sha256(convert_to(tok_b, 'UTF8'))) is not null, 'B''s token works');
  reset role;
  update auth.users set banned_until = now() + interval '1 day' where id = b;
  perform public.t_as_service();
  perform public.t_expect(public.use_api_token(sha256(convert_to(tok_b, 'UTF8'))) is null, 'a banned account''s token is refused');
  reset role;
  update auth.users set banned_until = null, deleted_at = now() where id = b;
  perform public.t_as_service();
  perform public.t_expect(public.use_api_token(sha256(convert_to(tok_b, 'UTF8'))) is null, 'a deleted account''s token is refused');
  reset role;
  update auth.users set deleted_at = null where id = b;

  -- ---------------------------------------------------------------- revoke keeps the record
  perform public.t_as_user(b);
  select (select id from public.my_api_tokens limit 1) into id_a;
  n := (select count(*) from public.my_api_tokens);
  perform public.t_expect(public.revoke_api_token(id_a) = true, 'B revokes their token');
  perform public.t_expect(public.revoke_api_token(id_a) = false, '... a second revoke says false');
  perform public.t_expect((select count(*) from public.my_api_tokens) = n, 'revoking deletes nothing');
  perform public.t_expect((select status from public.my_api_tokens where id = id_a) = 'revoked', '... the list says revoked');
  perform public.t_expect(public.revoke_api_token(gen_random_uuid()) = false, 'an unknown id gives false, not an error');
  reset role;
  perform public.t_expect((select revoked_at from public.api_tokens where id = id_a) is not null, 'revoked_at is set');
  perform public.t_as_service();
  perform public.t_expect(public.use_api_token(sha256(convert_to(tok_b, 'UTF8'))) is null, 'a revoked token is refused');
  reset role;

  -- ---------------------------------------------------------------- limits
  delete from public.api_tokens;
  perform public.t_as_user(a);
  for i in 1..10 loop
    seen := seen || (public.create_api_token('t' || i, 30) ->> 'token');
  end loop;
  perform public.t_expect((select count(distinct t) from unnest(seen) t) = 10, '10 tokens are all different');
  perform public.t_expect((select bool_and(t ~ '^rxd_pat_[A-Za-z0-9_-]{43}$') from unnest(seen) t), '... and all well-formed');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('eleventh', 30)$s$, 'at most 10 active'),
    'the 11th active token is refused');
  select id into id_a from public.my_api_tokens limit 1;
  perform public.revoke_api_token(id_a);
  perform public.create_api_token('after revoke', 30);
  perform public.t_expect(true, 'revoking one makes room');
  reset role;
  -- expired tokens do not count as active
  update public.api_tokens set created_at = now() - interval '40 days', expires_at = now() - interval '10 days' where name in ('t1', 't2');
  perform public.t_as_user(a);
  perform public.create_api_token('after expiry', 30);
  perform public.t_expect(true, 'expired tokens do not count against the 10');
  reset role;

  -- the daily cap: 20 creations in 24 hours, counting revoked ones
  delete from public.api_tokens where user_id = a;
  insert into public.api_tokens (user_id, name, hint, token_hash, expires_at, revoked_at)
    select a, 'old' || i, 'abcd', sha256(convert_to('seed' || i, 'UTF8')), now() + interval '30 days', now()
      from generate_series(1, 20) i;
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused($s$select public.create_api_token('21st today', 30)$s$, 'at most 20'),
    'a 21st token within 24 hours is refused');
  reset role;
  update public.api_tokens set created_at = now() - interval '2 days', expires_at = now() + interval '30 days' where user_id = a;
  perform public.t_as_user(a);
  perform public.create_api_token('next day', 30);
  perform public.t_expect(true, '... and fine again once they are older than a day');
  reset role;

  -- the cap on "on record" rows: 200 that are active or ended in the last 30 days
  delete from public.api_tokens where user_id = a;
  insert into public.api_tokens (user_id, name, hint, token_hash, created_at, expires_at, revoked_at)
    select a, 'old' || i, 'abcd', sha256(convert_to('bulk' || i, 'UTF8')), now() - interval '10 days', now() + interval '30 days', now() - interval '5 days'
      from generate_series(1, 200) i;
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused($s$select public.create_api_token('201st', 30)$s$, 'too many access tokens'),
    '200 tokens made or ended in the last 30 days is refused, and the error says so');
  perform public.t_expect(public.t_refused($s$select public.create_api_token('201st', 30)$s$, 'last 30 days'), '... naming the real window');
  reset role;
  -- the lockout the review found: old rows that ended long ago must not count (users cannot delete them)
  update public.api_tokens set created_at = now() - interval '300 days', expires_at = now() - interval '200 days',
    revoked_at = now() - interval '250 days' where user_id = a;
  perform public.t_as_user(a);
  perform public.create_api_token('after a long time', 30);
  perform public.t_expect(true, 'rows that ended more than 30 days ago do not count against the user');
  perform public.t_expect((select count(*) from public.api_tokens where user_id = a) = 201, '... and nothing was deleted');
  reset role;
  -- and another user is unaffected by A's limits
  perform public.t_as_user(b);
  perform public.create_api_token('b again', 30);
  perform public.t_expect(true, 'the limits are per user');
  reset role;

  -- ---------------------------------------------------------------- constraints (as the owner)
  perform public.t_expect(public.t_refused(
    format($s$insert into public.api_tokens (user_id, name, hint, token_hash, expires_at) values (%L, 'dup', 'abcd', sha256(convert_to('bulk1', 'UTF8')), now() + interval '1 day')$s$, a), '23505'),
    'the same hash cannot be stored twice');
  perform public.t_expect(public.t_refused(
    format($s$insert into public.api_tokens (user_id, name, hint, token_hash, expires_at) values (%L, 'far', 'abcd', sha256('far'), now() + interval '400 days')$s$, a), '23514'),
    'the database itself refuses an expiry beyond 365 days');
  perform public.t_expect(public.t_refused(
    format($s$insert into public.api_tokens (user_id, name, hint, token_hash, expires_at) values (%L, 'short', 'abcd', repeat('x', 10)::bytea, now() + interval '1 day')$s$, a), '23514'),
    'a hash must be 32 bytes');

  -- ---------------------------------------------------------------- the rate counter
  perform public.t_as_service();
  perform public.t_expect(public.api_token_rate_hit('ip:test', 3, 60), 'hit 1 of 3');
  perform public.t_expect(public.api_token_rate_hit('ip:test', 3, 60), 'hit 2 of 3');
  perform public.t_expect(public.api_token_rate_hit('ip:test', 3, 60), 'hit 3 of 3');
  perform public.t_expect(not public.api_token_rate_hit('ip:test', 3, 60), 'hit 4 is over the limit');
  perform public.t_expect(public.api_token_rate_hit('ip:other', 3, 60), 'another bucket is independent');
  perform public.t_expect(public.t_refused($s$select public.api_token_rate_hit('', 0, 60)$s$, '22023'), 'bad arguments are refused');
  perform public.t_expect(public.t_refused(format($s$select public.api_token_rate_hit(%L, 3, 60)$s$, repeat('k', 101)), '22023'), 'an over-long key is refused');
  reset role;
  update public.api_token_rate set window_start = now() - interval '2 minutes' where bucket = 'ip:test';
  perform public.t_as_service();
  perform public.t_expect(public.api_token_rate_hit('ip:test', 3, 60), 'a new window starts after it elapsed');
  reset role;

  -- ---------------------------------------------------------------- peek: reads, never counts
  perform public.t_as_service();
  perform public.t_expect(public.api_token_rate_peek('fail:x', 2, 60), 'an unused bucket is under its limit');
  perform public.t_expect(public.api_token_rate_peek('fail:x', 2, 60), '... peeking does not count');
  perform public.api_token_rate_hit('fail:x', 2, 60);
  perform public.api_token_rate_hit('fail:x', 2, 60);
  perform public.t_expect(not public.api_token_rate_peek('fail:x', 2, 60), 'a bucket at its limit is not under it');
  perform public.t_expect(public.t_refused($s$select public.api_token_rate_peek('', 0, 60)$s$, '22023'), 'peek refuses bad arguments');
  reset role;
  update public.api_token_rate set window_start = now() - interval '2 minutes' where bucket = 'fail:x';
  perform public.t_as_service();
  perform public.t_expect(public.api_token_rate_peek('fail:x', 2, 60), 'a bucket whose window elapsed is under its limit again');
  reset role;
  perform public.t_as_user(a);
  perform public.t_expect(public.t_refused($s$select public.api_token_rate_peek('k', 1, 60)$s$, '42501'), 'a signed-in user cannot call peek');
  reset role;
  perform public.t_expect((select count(*) from pg_indexes where tablename = 'api_token_rate' and indexdef like '%window_start%') = 1,
    'the rate table has an index on window_start (its cleanup never scans the table)');

  -- ---------------------------------------------------------------- a password change revokes the tokens
  delete from public.api_tokens where user_id = b;
  perform public.t_as_user(b);
  perform public.create_api_token('before reset', 30);
  perform public.create_api_token('another', 30);
  reset role;
  update auth.users set raw_user_meta_data = '{"x":1}'::jsonb where id = b;
  perform public.t_expect((select count(*) from public.api_tokens where user_id = b and revoked_at is null) = 2,
    'a change that is not a password change leaves the tokens alone');
  update auth.users set encrypted_password = 'new-hash' where id = b;
  perform public.t_expect((select count(*) from public.api_tokens where user_id = b and revoked_at is null) = 0,
    'changing the password revokes every token of the account');
  perform public.t_expect((select count(*) from public.api_tokens where user_id = b) = 2, '... and deletes nothing');
  update auth.users set encrypted_password = 'new-hash' where id = b;
  perform public.t_expect(true, 'the same value again is not a change');
  perform public.t_expect((select count(*) from public.api_tokens where user_id = a and revoked_at is null) > 0, 'another account''s tokens are untouched');

  -- ---------------------------------------------------------------- retention
  perform public.t_as_service();
  perform public.t_expect(public.t_refused($s$select public.purge_api_tokens(1)$s$, '22023'), 'the purge refuses less than 30 days');
  reset role;
  update public.api_tokens set expires_at = now() - interval '500 days', created_at = now() - interval '600 days', revoked_at = null
    where user_id = a and name = 'old1';
  perform public.t_as_service();
  perform public.t_expect(public.purge_api_tokens(400) = 1, 'the purge removes only tokens that ended long ago');
  reset role;

  -- ---------------------------------------------------------------- account deletion
  perform public.t_as_user(b);
  perform public.delete_my_account();
  reset role;
  perform public.t_expect((select count(*) from public.api_tokens where user_id = b) = 0, 'deleting the account deletes its tokens');
  perform public.t_expect((select count(*) from public.api_tokens where user_id = a) > 0, '... and only that user''s');
end
$t$;

rollback;
\echo ALL API TOKEN CHECKS PASSED
