-- radarx personal access tokens: how batch jobs, cron, CI and cluster nodes sign in without a
-- password or a browser (docs/ACCOUNTS.md "Access tokens for automation").
--
-- Design rules
--   * A token is `rxd_pat_` + 43 URL-safe base64 characters (256 random bits from pgcrypto). The
--     plaintext exists in exactly one place: the answer of create_api_token(), once. The database
--     stores only its SHA-256 (`token_hash`), which no client can read.
--   * A user can SELECT their own rows, but only the columns that are not the hash (column grants
--     plus row-level security, and a view for convenience). Nobody but the SECURITY DEFINER
--     functions below can insert, update or delete: no client privilege, no policy.
--   * A token has a name, an expiry (default 90 days, at most 365), last_used_at and revoked_at.
--     Revoking deletes nothing (the audit trail stays). At most 10 ACTIVE tokens per user, 200
--     rows that are active or ended in the last 30 days (older ended rows do not count, so an old
--     record can never lock someone out), 20 creations per 24 hours.
--   * Changing the password revokes every token of the account (a trigger on auth.users): a
--     stolen password reset must not leave a token behind. "Sign out everywhere" does NOT revoke
--     tokens (GoTrue has no hook for it): revoke them in the app.
--   * The only way to turn a token into anything is the Edge Function `exchange-api-token`
--     (supabase/functions/), which holds the service-role key and calls use_api_token(). The
--     answer is a short-lived entitlement token that is NOT a database token: row-level security
--     never accepts it.
--   * Deleting the account deletes its tokens (on delete cascade).

create extension if not exists pgcrypto with schema extensions;

create table public.api_tokens (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users (id) on delete cascade,
  name         text not null check (name ~ '^[A-Za-z0-9_ .+-]{1,64}$'),
  -- first four characters after the prefix: enough for a person to recognise a token in a list,
  -- 24 of its 256 bits
  hint         text not null check (hint ~ '^[A-Za-z0-9_-]{4}$'),
  token_hash   bytea not null unique check (octet_length(token_hash) = 32),
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  last_used_at timestamptz,
  revoked_at   timestamptz,
  check (expires_at > created_at and expires_at <= created_at + interval '365 days 1 minute'),
  check (revoked_at is null or revoked_at >= created_at)
);

create index api_tokens_user_idx on public.api_tokens (user_id, created_at desc);

alter table public.api_tokens enable row level security;

create policy "api_tokens: read own" on public.api_tokens
  for select to authenticated using (user_id = (select auth.uid()));
-- no insert, update or delete policy: only the definer functions below change this table.

-- Supabase grants every new table to anon / authenticated; take it back, then give back exactly the
-- columns that are not the hash.
revoke all on public.api_tokens from public, anon, authenticated;
grant select (id, user_id, name, hint, created_at, expires_at, last_used_at, revoked_at)
  on public.api_tokens to authenticated;

-- what the app lists (an invoker view: row-level security and the column grants above still apply)
create view public.my_api_tokens with (security_invoker = true) as
select
  id, name, hint, created_at, expires_at, last_used_at, revoked_at,
  case
    when revoked_at is not null then 'revoked'
    when expires_at <= now() then 'expired'
    else 'active'
  end as status
from public.api_tokens;

revoke all on public.my_api_tokens from public, anon, authenticated;
grant select on public.my_api_tokens to authenticated;

-- ---------------------------------------------------------------------------------------------
-- create: the plaintext is returned here and nowhere else, once

create or replace function public.create_api_token(p_name text, p_days int default 90)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
  claims jsonb := nullif(current_setting('request.jwt.claims', true), '')::jsonb;
  clean_name text := btrim(coalesce(p_name, ''));
  raw bytea;
  secret text;
  plain text;
  new_id uuid;
  new_expiry timestamptz;
  active_count int;
  recent_count_all int;
  recent_count int;
begin
  -- a real signed-in account only: not anonymous, not the service role, not another database role
  if uid is null or claims is null or claims ->> 'role' is distinct from 'authenticated'
     or coalesce((claims ->> 'is_anonymous')::boolean, false) then
    raise exception 'sign in to create an access token' using errcode = '28000';
  end if;
  if clean_name !~ '^[A-Za-z0-9_ .+-]{1,64}$' then
    raise exception 'the token name must be 1 to 64 plain characters (letters, digits, space, _ . + -)'
      using errcode = '22023';
  end if;
  if p_days is null or p_days < 1 or p_days > 365 then
    raise exception 'a token lasts 1 to 365 days' using errcode = '22023';
  end if;

  -- one creation at a time per user, so the limits below cannot be raced past
  perform pg_advisory_xact_lock(hashtextextended('api_token:' || uid::text, 0));

  -- "on record" means active, or ended (revoked or expired) within the last 30 days: users cannot
  -- delete rows, so rows that ended long ago must not count against them
  select count(*) filter (where revoked_at is null and expires_at > now()),
         count(*) filter (where least(expires_at, coalesce(revoked_at, expires_at)) > now() - interval '30 days'),
         count(*) filter (where created_at > now() - interval '24 hours')
    into active_count, recent_count_all, recent_count
    from public.api_tokens where user_id = uid;
  if active_count >= 10 then
    raise exception 'at most 10 active access tokens: revoke one first' using errcode = '54000';
  end if;
  if recent_count_all >= 200 then
    raise exception 'too many access tokens made or ended in the last 30 days (limit 200): try again when older ones have aged out'
      using errcode = '54000';
  end if;
  if recent_count >= 20 then
    raise exception 'at most 20 access tokens per day' using errcode = '54000';
  end if;

  raw := extensions.gen_random_bytes(32);
  -- standard base64 of 32 bytes is 43 characters and one "="; make it URL-safe and drop the "="
  secret := translate(encode(raw, 'base64'), '+/=', '-_');
  plain := 'rxd_pat_' || secret;
  new_expiry := now() + make_interval(days => p_days);

  insert into public.api_tokens (user_id, name, hint, token_hash, expires_at)
  values (uid, clean_name, left(secret, 4), sha256(convert_to(plain, 'UTF8')), new_expiry)
  returning id into new_id;

  return jsonb_build_object(
    'id', new_id, 'token', plain, 'name', clean_name, 'expires_at', new_expiry
  );
end;
$$;

revoke all on function public.create_api_token(text, int) from public, anon, authenticated;
grant execute on function public.create_api_token(text, int) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- revoke: marks, deletes nothing. False for a token that is not yours or already revoked.

create or replace function public.revoke_api_token(p_id uuid)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := auth.uid();
  claims jsonb := nullif(current_setting('request.jwt.claims', true), '')::jsonb;
  n int;
begin
  if uid is null or claims is null or claims ->> 'role' is distinct from 'authenticated' then
    raise exception 'sign in to revoke an access token' using errcode = '28000';
  end if;
  update public.api_tokens
     set revoked_at = now()
   where id = p_id and user_id = uid and revoked_at is null;
  get diagnostics n = row_count;
  return n > 0;
end;
$$;

revoke all on function public.revoke_api_token(uuid) from public, anon, authenticated;
grant execute on function public.revoke_api_token(uuid) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- service role only: look a token up by the SHA-256 of what was presented (the Edge Function).
-- Returns null for an unknown, revoked or expired token, or an account that is banned or deleted:
-- the caller cannot tell these apart, and neither can the person who presented it.

create or replace function public.use_api_token(p_hash bytea)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  r record;
begin
  if p_hash is null or octet_length(p_hash) <> 32 then
    return null;
  end if;
  select t.id, t.user_id, t.name, t.expires_at, t.last_used_at, u.email
    into r
    from public.api_tokens t
    join auth.users u on u.id = t.user_id
   where t.token_hash = p_hash
     and t.revoked_at is null
     and t.expires_at > now()
     and (u.banned_until is null or u.banned_until <= now())
     and u.deleted_at is null;
  if not found then
    return null;
  end if;
  -- at most one write a minute per token, however often it is exchanged
  if r.last_used_at is null or r.last_used_at < now() - interval '1 minute' then
    update public.api_tokens set last_used_at = now() where id = r.id;
  end if;
  return jsonb_build_object(
    'id', r.id, 'user_id', r.user_id, 'email', r.email, 'name', r.name, 'expires_at', r.expires_at
  );
end;
$$;

revoke all on function public.use_api_token(bytea) from public, anon, authenticated;
grant execute on function public.use_api_token(bytea) to service_role;

-- ---------------------------------------------------------------------------------------------
-- service role only: a fixed-window counter for the Edge Functions' rate limits (per client
-- address and per token hash; the keys are hashes, never an address or a token)

create table public.api_token_rate (
  bucket       text primary key check (char_length(bucket) <= 100),
  window_start timestamptz not null default now(),
  hits         int not null default 0
);
create index api_token_rate_window_idx on public.api_token_rate (window_start);
alter table public.api_token_rate enable row level security;
revoke all on public.api_token_rate from public, anon, authenticated;

create or replace function public.api_token_rate_hit(p_bucket text, p_limit int, p_window_s int)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  n int;
begin
  if p_bucket is null or char_length(p_bucket) > 100 or p_limit < 1 or p_window_s < 1 then
    raise exception 'bad rate-limit arguments' using errcode = '22023';
  end if;
  insert into public.api_token_rate as r (bucket, window_start, hits)
  values (p_bucket, now(), 1)
  on conflict (bucket) do update
    set window_start = case when r.window_start < now() - make_interval(secs => p_window_s)
                            then now() else r.window_start end,
        hits = case when r.window_start < now() - make_interval(secs => p_window_s)
                    then 1 else r.hits + 1 end
  returning hits into n;
  if random() < 0.02 then  -- tidy up now and then: an index range scan, a bounded batch
    delete from public.api_token_rate
     where bucket in (select bucket from public.api_token_rate
                       where window_start < now() - interval '1 day' limit 200);
  end if;
  return n <= p_limit;
end;
$$;

revoke all on function public.api_token_rate_hit(text, int, int) from public, anon, authenticated;
grant execute on function public.api_token_rate_hit(text, int, int) to service_role;

-- is the bucket still under its limit? (reads, never counts: for limits on FAILURES, which are
-- checked before the work and counted after it)
create or replace function public.api_token_rate_peek(p_bucket text, p_limit int, p_window_s int)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  r record;
begin
  if p_bucket is null or char_length(p_bucket) > 100 or p_limit < 1 or p_window_s < 1 then
    raise exception 'bad rate-limit arguments' using errcode = '22023';
  end if;
  select window_start, hits into r from public.api_token_rate where bucket = p_bucket;
  if not found or r.window_start < now() - make_interval(secs => p_window_s) then
    return true;
  end if;
  return r.hits < p_limit;
end;
$$;

revoke all on function public.api_token_rate_peek(text, int, int) from public, anon, authenticated;
grant execute on function public.api_token_rate_peek(text, int, int) to service_role;

-- ---------------------------------------------------------------------------------------------
-- retention: tokens that ended (revoked or expired) more than `days` ago may be removed. The owner
-- schedules this if they want to (nothing does it automatically).

create or replace function public.purge_api_tokens(days int default 400)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  n bigint;
begin
  if days is null or days < 30 then
    raise exception 'days must be at least 30' using errcode = '22023';
  end if;
  delete from public.api_tokens
   where least(expires_at, coalesce(revoked_at, expires_at)) < now() - make_interval(days => days);
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function public.purge_api_tokens(int) from public, anon, authenticated;
grant execute on function public.purge_api_tokens(int) to service_role;

-- ---------------------------------------------------------------------------------------------
-- a password change (a reset included) revokes every token of the account. The trigger can never
-- block the password change itself: a failure here is swallowed.

create or replace function public.revoke_api_tokens_on_password_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    update public.api_tokens set revoked_at = now()
     where user_id = new.id and revoked_at is null;
  exception when others then
    null;
  end;
  return new;
end;
$$;

revoke all on function public.revoke_api_tokens_on_password_change() from public, anon, authenticated;

create trigger api_tokens_revoke_on_password_change
  after update of encrypted_password on auth.users
  for each row
  when (old.encrypted_password is distinct from new.encrypted_password)
  execute function public.revoke_api_tokens_on_password_change();
