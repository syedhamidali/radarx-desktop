-- radarx accounts: profiles, devices and synced items. (No billing: that is a later migration.)
--
-- Design rules
--   * The app only ever holds the PUBLISHABLE key. Every table has row-level security (RLS); a
--     signed-in user can see and change only their own rows.
--   * Every account is a full account. Plans, subscriptions and entitlements are deliberately NOT
--     here yet; they arrive in their own migration when billing is designed.
--   * Nothing here is needed to run the science: the engine and the CLI never talk to this.

-- ---------------------------------------------------------------------------------------------
-- helpers

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- profiles: one row per auth user, created by a trigger on sign-up

create table public.profiles (
  id                uuid primary key references auth.users (id) on delete cascade,
  display_name      text check (char_length(display_name) <= 120),
  avatar_url        text check (char_length(avatar_url) <= 500),
  -- university, agency, company or "independent"; asked at sign-up, never shown to others
  affiliation       text check (char_length(affiliation) <= 160),
  -- why they use radarx; informs the roadmap, never shown to others
  use_case          text check (use_case in ('research', 'operations', 'teaching', 'hydrology', 'hobby', 'other')),
  terms_version     text,
  terms_accepted_at timestamptz,
  marketing_opt_in  boolean not null default false,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "profiles: read own" on public.profiles
  for select to authenticated using (id = (select auth.uid()));
create policy "profiles: update own" on public.profiles
  for update to authenticated using (id = (select auth.uid())) with check (id = (select auth.uid()));
-- no insert policy (the trigger creates the row) and no delete policy (deleting the auth user
-- cascades)

create trigger profiles_touch before update on public.profiles
  for each row execute function public.touch_updated_at();

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  meta jsonb := coalesce(new.raw_user_meta_data, '{}'::jsonb);
  name text := nullif(btrim(coalesce(meta ->> 'full_name', meta ->> 'name', '')), '');
begin
  insert into public.profiles (id, display_name, avatar_url, affiliation, use_case, terms_version, terms_accepted_at)
  values (
    new.id,
    left(coalesce(name, split_part(coalesce(new.email, ''), '@', 1)), 120),
    left(nullif(coalesce(meta ->> 'avatar_url', meta ->> 'picture', ''), ''), 500),
    left(nullif(btrim(coalesce(meta ->> 'affiliation', '')), ''), 160),
    case when meta ->> 'use_case' in ('research', 'operations', 'teaching', 'hydrology', 'hobby', 'other')
         then meta ->> 'use_case' end,
    nullif(meta ->> 'terms_version', ''),
    case when nullif(meta ->> 'terms_version', '') is not null then now() end
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------------------------
-- devices: the installs signed in to an account (lets the user see and remove them)

create table public.devices (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users (id) on delete cascade,
  device_id   text not null check (char_length(device_id) between 8 and 80),  -- random, made on first run
  name        text check (char_length(name) <= 120),
  platform    text check (char_length(platform) <= 40),
  app_version text check (char_length(app_version) <= 40),
  first_seen  timestamptz not null default now(),
  last_seen   timestamptz not null default now(),
  unique (user_id, device_id)
);

alter table public.devices enable row level security;
create policy "devices: read own" on public.devices
  for select to authenticated using (user_id = (select auth.uid()));
create policy "devices: remove own" on public.devices
  for delete to authenticated using (user_id = (select auth.uid()));
-- inserts and updates go through register_device()

-- Records this install (or refreshes its last_seen). Returns {registered, count}.
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
  cnt int;
begin
  if uid is null then
    raise exception 'not signed in' using errcode = '28000';
  end if;
  insert into public.devices (user_id, device_id, name, platform, app_version)
  values (uid, p_device_id, left(p_name, 120), left(p_platform, 40), left(p_app_version, 40))
  on conflict (user_id, device_id)
  do update set last_seen = now(),
                name = coalesce(excluded.name, public.devices.name),
                platform = coalesce(excluded.platform, public.devices.platform),
                app_version = coalesce(excluded.app_version, public.devices.app_version);
  select count(*) into cnt from public.devices where user_id = uid;
  return jsonb_build_object('registered', true, 'count', cnt);
end;
$$;

revoke all on function public.register_device(text, text, text, text) from public, anon;
grant execute on function public.register_device(text, text, text, text) to authenticated;

-- ---------------------------------------------------------------------------------------------
-- synced_items: recipes, workspaces, looks and colour maps the user chose to keep in the cloud

create table public.synced_items (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users (id) on delete cascade,
  kind        text not null check (kind in ('recipe', 'workspace', 'look', 'colormap', 'settings')),
  name        text not null check (char_length(name) between 1 and 160),
  data        jsonb not null check (pg_column_size(data) <= 262144),    -- 256 KB per item
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, kind, name)
);

create index synced_items_user_kind_idx on public.synced_items (user_id, kind);

alter table public.synced_items enable row level security;
create policy "synced_items: read own" on public.synced_items
  for select to authenticated using (user_id = (select auth.uid()));
create policy "synced_items: add own" on public.synced_items
  for insert to authenticated with check (user_id = (select auth.uid()));
create policy "synced_items: change own" on public.synced_items
  for update to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy "synced_items: delete own" on public.synced_items
  for delete to authenticated using (user_id = (select auth.uid()));

create trigger synced_items_touch before update on public.synced_items
  for each row execute function public.touch_updated_at();

-- A fixed quota that keeps one account from filling the database; enforced where it cannot be skipped.
create or replace function public.enforce_synced_items_limit()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  lim constant int := 500;
  cnt int;
begin
  select count(*) into cnt from public.synced_items where user_id = new.user_id;
  if cnt >= lim then
    raise exception 'synced item limit (%) reached', lim using errcode = 'P0001';
  end if;
  return new;
end;
$$;

create trigger synced_items_limit before insert on public.synced_items
  for each row execute function public.enforce_synced_items_limit();

-- ---------------------------------------------------------------------------------------------
-- deleting an account (a right under GDPR / CCPA): the user removes their own auth row; every
-- table above cascades. Called from the Account dialog after a typed confirmation.

create or replace function public.delete_my_account()
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  uid uuid := (select auth.uid());
begin
  if uid is null then
    raise exception 'not signed in' using errcode = '28000';
  end if;
  delete from auth.users where id = uid;
end;
$$;

revoke all on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;
