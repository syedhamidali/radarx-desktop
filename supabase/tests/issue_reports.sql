-- Security and behaviour checks for supabase/migrations/20261024000000_issue_reports.sql.
-- Run after the earlier migrations, against a LOCAL stack only:
--   psql "postgresql://postgres:postgres@127.0.0.1:55322/postgres" -v ON_ERROR_STOP=1 -f supabase/tests/issue_reports.sql
-- One transaction, rolled back at the end.
--
-- What this CANNOT show: the advisory lock in insert_issue_report and the single-statement atomicity of
-- take_issue_limit exist for CONCURRENT transactions, and a test script runs on one connection. The lock is
-- tested by its presence in the function body, the caps by behaviour.
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
  if ok is not true then raise exception 'FAILED: %', what; end if;
  insert into public.t_log values (what);
  raise notice 'ok   %', what;
end $$;

-- Runs a statement as the current role and says whether it was refused WITH THE EXPECTED ERROR
-- (a SQLSTATE such as '23514' or a fragment of the message). A different error raises instead of passing.
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
  c uuid := gen_random_uuid();
  d uuid := gen_random_uuid();
  ra uuid;
  rb uuid;
  rw uuid;
  n int;
  cols text;
  ok boolean;
  base text := $b$insert into public.issue_reports (source, kind, title, description) values $b$;
begin
  insert into auth.users (id, email) values (a, 'a@example.test'), (b, 'b@example.test'), (c, 'c@example.test'), (d, 'd@example.test');

  -- ------------------------------------------------------------------ the service role stores reports
  execute 'set local role service_role';
  ra := public.insert_issue_report(a, 'app', 'bug', 'A crash on open', 'The app closes when I open a file.',
                                   null, '0.3.0', 'macOS arm64', null, 'diag line 1');
  rb := public.insert_issue_report(b, 'app', 'idea', 'A dark map', 'Please add a darker base map.');
  rw := public.insert_issue_report(null, 'web', 'question', 'How do I install', 'Where do I find the Windows installer?',
                                   'visitor@example.org');
  reset role;
  perform public.t_expect((select count(*) from public.issue_reports) = 3, 'the service role can store app and web reports');
  perform public.t_expect((select status from public.issue_reports where id = ra) = 'new', '... a new report has the status "new"');
  perform public.t_expect((select user_id from public.issue_reports where id = rw) is null, '... a web report has no user');
  perform public.t_expect((select app_version from public.issue_reports where id = ra) = '0.3.0', '... optional fields are kept');

  -- ------------------------------------------------------------------ RLS is on, policies are exactly what we meant
  perform public.t_expect((select relrowsecurity from pg_class where oid = 'public.issue_reports'::regclass), 'RLS is on for issue_reports');
  perform public.t_expect((select relrowsecurity from pg_class where oid = 'public.issue_report_limits'::regclass), 'RLS is on for issue_report_limits');
  perform public.t_expect((select count(*) from pg_policies where tablename = 'issue_reports') = 1
    and (select cmd from pg_policies where tablename = 'issue_reports') = 'SELECT', 'issue_reports has exactly one policy, for SELECT');
  perform public.t_expect((select count(*) from pg_policies where tablename = 'issue_report_limits') = 0, 'issue_report_limits has no policy at all');

  -- ------------------------------------------------------------------ a signed-in user: reads their own, writes nothing
  perform public.t_as_user(a);
  perform public.t_expect((select count(*) from public.issue_reports) = 1 and (select id from public.issue_reports) = ra,
    'A reads exactly their own report (not B''s, not the web one)');
  perform public.t_expect((select count(*) from public.issue_reports where user_id = b) = 0, '... asking for B''s rows returns nothing');
  perform public.t_expect(public.t_refused(base || $s$('app', 'bug', 'Sneaky insert', 'Inserted straight into the table.')$s$, '42501'),
    'A cannot insert a report directly');
  perform public.t_expect(public.t_refused($s$update public.issue_reports set status = 'closed'$s$, '42501'), 'A cannot update a report (not even their own)');
  perform public.t_expect(public.t_refused($s$update public.issue_reports set title = 'Changed title' where id = '$s$ || rb || $s$'$s$, '42501'), 'A cannot update B''s report');
  perform public.t_expect(public.t_refused('delete from public.issue_reports', '42501'), 'A cannot delete reports');
  perform public.t_expect(public.t_refused('select * from public.issue_report_limits', '42501'), 'A cannot read the rate-limit table');
  perform public.t_expect(public.t_refused('insert into public.issue_report_limits values (''x'', now(), 1)', '42501'), 'A cannot write the rate-limit table');
  perform public.t_expect(public.t_refused('select * from public.issue_reports_triage', '42501'), 'A cannot read the owner triage view');
  perform public.t_expect(public.t_refused($s$select public.take_issue_limit('ip:x', now(), 5)$s$, '42501'), 'A cannot call take_issue_limit');
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report(null, 'web', 'bug', 'Via rpc', 'Calling the function directly.')$s$, '42501'),
    'A cannot call insert_issue_report (no cap bypass, no web reports)');
  perform public.t_expect(public.t_refused('select public.purge_issue_reports(1)', '42501'), 'A cannot call purge_issue_reports');
  reset role;
  perform public.t_expect((select count(*) from public.issue_reports) = 3, '... and nothing changed');

  perform public.t_as_user(b);
  perform public.t_expect((select count(*) from public.issue_reports) = 1 and (select id from public.issue_reports) = rb, 'B reads exactly their own report');
  reset role;

  -- ------------------------------------------------------------------ anon: nothing at all
  execute 'set local role anon';
  perform public.t_expect(public.t_refused('select * from public.issue_reports', '42501'), 'anon cannot read issue_reports');
  perform public.t_expect(public.t_refused(base || $s$('web', 'bug', 'Anon insert', 'Inserted straight into the table.')$s$, '42501'), 'anon cannot insert');
  perform public.t_expect(public.t_refused('update public.issue_reports set status = ''closed''', '42501'), 'anon cannot update');
  perform public.t_expect(public.t_refused('delete from public.issue_reports', '42501'), 'anon cannot delete');
  perform public.t_expect(public.t_refused('select * from public.issue_report_limits', '42501'), 'anon cannot read the rate-limit table');
  perform public.t_expect(public.t_refused('select * from public.issue_reports_triage', '42501'), 'anon cannot read the triage view');
  perform public.t_expect(public.t_refused($s$select public.take_issue_limit('ip:x', now(), 5)$s$, '42501'), 'anon cannot call take_issue_limit');
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report(null, 'web', 'bug', 'Via rpc', 'Calling the function directly.')$s$, '42501'), 'anon cannot call insert_issue_report');
  perform public.t_expect(public.t_refused('select public.purge_issue_reports(1)', '42501'), 'anon cannot call purge_issue_reports');
  reset role;

  -- EXECUTE itself is not granted to the client roles (checked directly, not only through what the call can do)
  perform public.t_expect(not exists (
      select 1 from (values ('public.take_issue_limit(text,timestamptz,int)'),
                            ('public.insert_issue_report(uuid,text,text,text,text,text,text,text,text,text,int,text,int)'),
                            ('public.purge_issue_reports(int)')) f(sig), (values ('anon'), ('authenticated')) r(rolname)
       where has_function_privilege(r.rolname, f.sig::regprocedure, 'execute')),
    'anon and authenticated have no EXECUTE on take_issue_limit, insert_issue_report, purge_issue_reports');
  perform public.t_expect(not has_table_privilege('anon', 'public.issue_reports', 'select')
    and not has_table_privilege('authenticated', 'public.issue_reports', 'insert')
    and not has_table_privilege('authenticated', 'public.issue_reports', 'update')
    and not has_table_privilege('authenticated', 'public.issue_reports', 'delete')
    and has_table_privilege('authenticated', 'public.issue_reports', 'select'),
    'table privileges: anon none; authenticated SELECT only');

  -- ------------------------------------------------------------------ the triage view
  execute 'set local role service_role';
  perform public.t_expect((select count(*) from public.issue_reports_triage) = 3, 'the service role reads the triage view (all reports)');
  perform public.t_expect((select has_diagnostics from public.issue_reports_triage where id = ra) and not (select has_diagnostics from public.issue_reports_triage where id = rb),
    '... it says which reports carry diagnostics');
  perform public.t_expect((select has_account from public.issue_reports_triage where id = ra) and not (select has_account from public.issue_reports_triage where id = rw),
    '... and which belong to an account');
  reset role;
  select string_agg(column_name, ',') into cols from information_schema.columns where table_schema = 'public' and table_name = 'issue_reports_triage';
  perform public.t_expect(not (',' || cols || ',' like '%,diagnostics,%' or ',' || cols || ',' like '%,user_id,%' or ',' || cols || ',' like '%,description,%'),
    '... it shows no diagnostics text, no user id and no full description (' || cols || ')');
  perform public.t_expect((select (reloptions)::text like '%security_invoker=true%' from pg_class where oid = 'public.issue_reports_triage'::regclass),
    '... it is a security-invoker view (RLS applies even if a grant slips)');

  -- ------------------------------------------------------------------ constraints (the second line behind the function)
  execute 'set local role service_role';
  perform public.t_expect(public.t_refused(base || $s$('app', 'bug', 'ab', 'A description long enough.')$s$, '23514'), 'a 2-character title is refused');
  perform public.t_expect(public.t_refused(base || $s$('app', 'bug', '    ', 'A description long enough.')$s$, '23514'), 'a blank title is refused');
  perform public.t_expect(public.t_refused(format(base || '(''app'', ''bug'', %L, ''A description long enough.'')', repeat('t', 141)), '23514'), 'a 141-character title is refused');
  perform public.t_expect(public.t_refused(base || $s$('app', 'bug', 'Good title', 'too short')$s$, '23514'), 'a 9-character description is refused');
  perform public.t_expect(public.t_refused(format(base || '(''app'', ''bug'', ''Good title'', %L)', repeat('d', 5001)), '23514'), 'a 5001-character description is refused');
  perform public.t_expect(public.t_refused(base || $s$('app', 'rant', 'Good title', 'A description long enough.')$s$, '23514'), 'an unknown kind is refused');
  perform public.t_expect(public.t_refused(base || $s$('mail', 'bug', 'Good title', 'A description long enough.')$s$, '23514'), 'an unknown source is refused');
  perform public.t_expect(public.t_refused($s$insert into public.issue_reports (source, kind, title, description, status) values ('app', 'bug', 'Good title', 'A description long enough.', 'wontfix')$s$, '23514'), 'an unknown status is refused');
  perform public.t_expect(public.t_refused($s$insert into public.issue_reports (source, kind, title, description, contact_email) values ('web', 'bug', 'Good title', 'A description long enough.', 'not-an-address')$s$, '23514'), 'a contact e-mail that is no address is refused');
  perform public.t_expect(public.t_refused($s$insert into public.issue_reports (source, kind, title, description, contact_email) values ('web', 'bug', 'Good title', 'A description long enough.', 'a b@example.org')$s$, '23514'), 'a contact e-mail with a space is refused');
  perform public.t_expect(public.t_refused($s$insert into public.issue_reports (source, kind, title, description, contact_email) values ('web', 'bug', 'Good title', 'A description long enough.', 'a@b.co"><img src=x>')$s$, '23514'), 'a contact e-mail with markup is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (source, kind, title, description, contact_email) values ('web', 'bug', 'Good title', 'A description long enough.', %L)$f$, repeat('a', 195) || '@b.org'), '23514'), 'a contact e-mail over 200 characters is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (source, kind, title, description, app_version) values ('web', 'bug', 'Good title', 'A description long enough.', %L)$f$, repeat('v', 41)), '23514'), 'a 41-character app_version is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (source, kind, title, description, os) values ('web', 'bug', 'Good title', 'A description long enough.', %L)$f$, repeat('o', 61)), '23514'), 'a 61-character os is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (source, kind, title, description, engine_version) values ('web', 'bug', 'Good title', 'A description long enough.', %L)$f$, repeat('e', 41)), '23514'), 'a 41-character engine_version is refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (source, kind, title, description, diagnostics) values ('web', 'bug', 'Good title', 'A description long enough.', %L)$f$, repeat('x', 20001)), '23514'), 'diagnostics over 20000 characters are refused');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (user_id, source, kind, title, description) values (%L, 'web', 'bug', 'Good title', 'A description long enough.')$f$, a), '23514'), 'a web report cannot carry a user id');
  perform public.t_expect(public.t_refused(format($f$insert into public.issue_reports (user_id, source, kind, title, description) values (%L, 'app', 'bug', 'Good title', 'A description long enough.')$f$, gen_random_uuid()), '23503'), 'a report for a user that does not exist is refused (foreign key)');
  -- the boundaries are accepted
  insert into public.issue_reports (source, kind, title, description, contact_email, app_version, os, engine_version, diagnostics)
    values ('web', 'bug', repeat('t', 140), repeat('d', 5000), 'a.b+c@sub.example.org', repeat('v', 40), repeat('o', 60), repeat('e', 40), repeat('x', 20000));
  insert into public.issue_reports (source, kind, title, description) values ('web', 'idea', 'abc', '0123456789');
  perform public.t_expect(true, 'the exact limits (140 / 5000 / 40 / 60 / 40 / 20000, 3 / 10) are accepted');
  reset role;

  -- ------------------------------------------------------------------ per-user cap: 10 per rolling 24 hours
  delete from public.issue_reports where id not in (ra, rb, rw);
  execute 'set local role service_role';
  for i in 1..9 loop
    perform public.insert_issue_report(b, 'app', 'bug', 'Report number ' || i, 'A report to fill the daily allowance.');
  end loop;
  perform public.t_expect((select count(*) from public.issue_reports where user_id = b) = 10, 'B can store 10 reports');
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report('$s$ || b || $s$', 'app', 'bug', 'One too many', 'The eleventh report within a day.')$s$, 'report limit'),
    'the 11th report within 24 hours is refused (P0001 "report limit")');
  perform public.t_expect((select count(*) from public.issue_reports where user_id = b) = 10, '... and nothing was stored');
  perform public.insert_issue_report(a, 'app', 'bug', 'A is unaffected', 'Another account has its own allowance.');
  perform public.t_expect((select count(*) from public.issue_reports where user_id = a) = 2, 'the cap is per account');
  reset role;
  update public.issue_reports set created_at = now() - interval '25 hours' where user_id = b and title in ('Report number 1', 'Report number 2');
  execute 'set local role service_role';
  perform public.insert_issue_report(b, 'app', 'bug', 'After the window', 'Reports older than a day stop counting.');
  perform public.t_expect((select count(*) from public.issue_reports where user_id = b and title = 'After the window') = 1, 'reports older than 24 hours do not count against the cap');
  perform public.t_expect(public.insert_issue_report(null, 'web', 'bug', 'Web is not capped here', 'The per-account cap applies to accounts only.') is not null, 'a web report (no user) is not subject to the per-account cap');
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report(null, 'web', 'bug', 'bad', 'too short')$s$, '23514'), 'the constraints apply inside the function too');
  reset role;
  perform public.t_expect(pg_get_functiondef('public.insert_issue_report(uuid,text,text,text,text,text,text,text,text,text,int,text,int)'::regprocedure) ~ '\n\s*perform pg_advisory_xact_lock',
    'insert_issue_report takes a per-user advisory lock (concurrency itself is not testable on one connection)');

  -- the global safety net sits behind the account's own cap
  execute 'set local role service_role';
  perform public.insert_issue_report(c, 'app', 'bug', 'Global one', 'First report counted in the global bucket.', null, null, null, null, null, 10, 'global:app', 2);
  perform public.insert_issue_report(d, 'app', 'bug', 'Global two', 'Second report counted in the global bucket.', null, null, null, null, null, 20, 'global:app', 2);
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report('$s$ || c || $s$', 'app', 'bug', 'Global three', 'Third report, over the global bucket.', null, null, null, null, null, 10, 'global:app', 2)$s$, 'global limit'),
    'the global daily bucket refuses a report when full (P0001 "global limit")');
  perform public.t_expect((select count(*) from public.issue_reports where title = 'Global three') = 0, '... and stores nothing');
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report(null, 'web', 'bug', 'No max given', 'A global key without a limit.', null, null, null, null, null, 10, 'global:web', null)$s$, '22023'),
    'a global key without a limit is refused');
  reset role;
  delete from public.issue_report_limits where key = 'global:app';
  delete from public.issue_reports where title like 'Global %';
  -- a full user cap must not consume the global bucket
  execute 'set local role service_role';
  perform public.insert_issue_report(c, 'app', 'bug', 'Global x', 'Fill the allowance of this account.', null, null, null, null, null, 1);
  perform public.t_expect(public.t_refused($s$select public.insert_issue_report('$s$ || c || $s$', 'app', 'bug', 'Global y', 'Over the account cap of one.', null, null, null, null, null, 1, 'global:app', 5)$s$, 'report limit'),
    'an account over its own cap is refused first');
  perform public.t_expect((select count(*) from public.issue_report_limits where key = 'global:app') = 0, '... without taking anything from the global bucket');
  reset role;
  delete from public.issue_reports where title like 'Global %';

  -- ------------------------------------------------------------------ rate-limit buckets
  execute 'set local role service_role';
  for i in 1..5 loop
    perform public.t_expect(public.take_issue_limit('ip:aaaa', date_trunc('hour', now()), 5), 'bucket use ' || i || ' of 5 is allowed');
  end loop;
  perform public.t_expect(not public.take_issue_limit('ip:aaaa', date_trunc('hour', now()), 5), 'the 6th use in the same hour is refused');
  perform public.t_expect((select count from public.issue_report_limits where key = 'ip:aaaa') = 5, '... the counter stays at the limit (no overflow)');
  perform public.t_expect(public.take_issue_limit('ip:bbbb', date_trunc('hour', now()), 5), 'another key has its own bucket');
  perform public.t_expect(public.take_issue_limit('ip:aaaa', date_trunc('hour', now()) + interval '1 hour', 5), 'the next hour is a new bucket');
  perform public.t_expect(not public.take_issue_limit('ip:cccc', date_trunc('hour', now()), 0), 'a limit of 0 refuses everything');
  perform public.t_expect(public.t_refused($s$select public.take_issue_limit('', now(), 5)$s$, '23514'), 'an empty key is refused');
  reset role;
  insert into public.issue_report_limits (key, bucket, count) values ('ip:old', now() - interval '4 days', 3);
  execute 'set local role service_role';
  perform public.take_issue_limit('ip:dddd', date_trunc('hour', now()), 5);
  reset role;
  perform public.t_expect(not exists (select 1 from public.issue_report_limits where key = 'ip:old'), 'buckets older than three days are deleted on use');

  -- ------------------------------------------------------------------ purge
  delete from public.issue_reports;
  insert into public.issue_reports (source, kind, title, description, created_at) values
    ('web', 'bug', 'Very old report', 'Created three years ago.', now() - interval '1100 days'),
    ('web', 'bug', 'Old report', 'Created about 25 months ago.', now() - interval '760 days'),
    ('web', 'bug', 'Recent report', 'Created two months ago.', now() - interval '60 days'),
    ('web', 'bug', 'New report', 'Created just now.', now());
  insert into public.issue_report_limits (key, bucket, count) values ('ip:stale', now() - interval '5 days', 1);
  execute 'set local role service_role';
  n := public.purge_issue_reports();
  reset role;
  perform public.t_expect(n = 2, 'purge_issue_reports() with the default 730 days deletes the two oldest reports (returned ' || n || ')');
  perform public.t_expect((select count(*) from public.issue_reports) = 2, '... the recent ones stay');
  perform public.t_expect(not exists (select 1 from public.issue_report_limits where key = 'ip:stale'), '... expired rate-limit buckets go too');
  execute 'set local role service_role';
  n := public.purge_issue_reports(30);
  reset role;
  perform public.t_expect(n = 1 and (select count(*) from public.issue_reports) = 1, 'purge_issue_reports(30) keeps only the newest');
  execute 'set local role service_role';
  perform public.t_expect(public.t_refused('select public.purge_issue_reports(0)', '22023'), 'purge_issue_reports(0) is refused (it would delete everything)');
  perform public.t_expect(public.t_refused('select public.purge_issue_reports(null)', '22023'), 'purge_issue_reports(null) is refused');
  reset role;
  perform public.t_expect((select count(*) from public.issue_reports) = 1, '... and nothing was deleted by the refused calls');

  -- ------------------------------------------------------------------ deleting an account keeps the report, unlinked
  delete from public.issue_reports;
  execute 'set local role service_role';
  ra := public.insert_issue_report(a, 'app', 'bug', 'A report from A', 'This report must survive A leaving.', 'a@example.test');
  rb := public.insert_issue_report(b, 'app', 'bug', 'A report from B', 'This report belongs to B who stays.');
  reset role;
  perform public.t_as_user(a);
  perform public.delete_my_account();
  reset role;
  perform public.t_expect(not exists (select 1 from auth.users where id = a), 'A deleted their account');
  perform public.t_expect(exists (select 1 from public.issue_reports where id = ra), '... the report is kept');
  perform public.t_expect((select user_id from public.issue_reports where id = ra) is null, '... but no longer linked to anyone');
  perform public.t_expect((select user_id from public.issue_reports where id = rb) = b, '... B''s report is untouched');
  perform public.t_as_user(b);
  perform public.t_expect((select count(*) from public.issue_reports) = 1, 'B still sees only their own (the unlinked report is nobody''s)');
  reset role;

  -- ------------------------------------------------------------------ housekeeping
  update public.issue_reports set updated_at = now() - interval '1 day', status = 'triaged' where id = rb;
  perform public.t_expect((select updated_at = now() and status = 'triaged' from public.issue_reports where id = rb),
    'the owner can triage a report and updated_at follows (service-level update)');
end
$t$;

rollback;
\echo ALL ISSUE REPORT CHECKS PASSED
