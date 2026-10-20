<script lang="ts">
  import { auth, signUp, signIn, signInWith, confirmCode, resendCode, resetPassword, completeReset, saveProfile, abortOAuth, signOut, USE_CASES, profileIncomplete, TERMS_VERSION } from '../stores/auth';
  import { OAUTH_PROVIDERS, OFFLINE_GRACE_DAYS } from '../auth/config';
  import { looksLikeEmail, passwordProblem } from '../auth/gate';
  import { TERMS_URL, PRIVACY_URL } from '../utils/brand';
  import LogoMark from './LogoMark.svelte';
  import RadarSweep from './RadarSweep.svelte';

  type Mode = 'signup' | 'signin' | 'forgot' | 'reset';
  let mode: Mode = $state('signup');
  let name = $state('');
  let email = $state('');
  let password = $state('');
  let affiliation = $state('');
  let independent = $state(false);
  let useCase = $state('research');
  let agree = $state(false);
  let code = $state('');
  let touched = $state(false);

  const a = $derived($auth);
  const completing = $derived(profileIncomplete(a));
  const waitingForCode = $derived(!!a.pendingEmail);
  const isSignup = $derived(mode === 'signup');

  // prefill from what the provider gave us when finishing a profile
  $effect(() => {
    if (completing && a.profile) {
      if (!name) name = a.profile.display_name ?? '';
      if (!affiliation && a.profile.affiliation) affiliation = a.profile.affiliation;
      if (a.profile.use_case) useCase = a.profile.use_case;
    }
  });

  const affiliationValue = $derived(independent ? 'Independent' : affiliation.trim());
  const problems = $derived.by(() => {
    const p: Record<string, string> = {};
    if (!completing) {
      if (!looksLikeEmail(email)) p.email = 'Enter a valid e-mail address.';
      if (mode !== 'forgot') {
        const pw = isSignup || mode === 'reset' ? passwordProblem(password) : password ? null : 'Enter your password.';
        if (pw) p.password = pw;
      }
      if (mode === 'reset' && code.trim().length < 6) p.code = 'Enter the code from the e-mail.';
    }
    if (isSignup || completing) {
      if (!name.trim()) p.name = 'Tell us what to call you.';
      if (!affiliationValue) p.affiliation = 'Enter your university, agency or company, or tick "Independent".';
      if (!agree) p.agree = 'Please accept the terms to continue.';
    }
    return p;
  });
  const valid = $derived(Object.keys(problems).length === 0);

  async function submit(e: Event) {
    e.preventDefault();
    touched = true;
    if (!valid || a.busy) return;
    if (completing) {
      await saveProfile({ display_name: name, affiliation: affiliationValue, use_case: useCase });
    } else if (mode === 'signup') {
      await signUp({ email, password, name, affiliation: affiliationValue, useCase });
    } else if (mode === 'signin') {
      await signIn(email, password);
    } else if (mode === 'forgot') {
      if (await resetPassword(email)) {
        mode = 'reset';
        touched = false;
        password = '';
      }
    } else {
      await completeReset(email, code, password); // success signs in: the gate closes by itself
    }
  }

  async function submitCode(e: Event) {
    e.preventDefault();
    if (a.pendingEmail && code.trim().length >= 6) await confirmCode(a.pendingEmail, code);
  }

  async function link(url: string) {
    try {
      const { open } = await import('@tauri-apps/plugin-shell');
      await open(url);
    } catch {
      window.open(url, '_blank', 'noopener');
    }
  }

</script>

<div class="gate" role="main">
  <div class="scope" aria-hidden="true"><RadarSweep size={560} period={14} rings={4} labels={false} hub={false} /></div>

  <div class="card brackets-4">
    <div class="brand">
      <LogoMark size={44} title="radarx" />
      <div>
        <div class="name"><span class="x">radar</span>x</div>
        <div class="sub">radar research workstation</div>
      </div>
    </div>

    {#if !a.ready || (a.gate === 'open' && a.profileState === 'loading')}
      <p class="note" role="status">Checking your account...</p>
    {:else if waitingForCode}
      <h1>Check your e-mail</h1>
      <p class="note">We sent a code to <strong>{a.pendingEmail}</strong>. Enter it to finish creating your account.</p>
      <form onsubmit={submitCode}>
        <label>
          <span>Code</span>
          <input class="code" inputmode="numeric" autocomplete="one-time-code" maxlength="10" bind:value={code} placeholder="123456" />
        </label>
        {#if a.error}<p class="err" role="alert">{a.error}</p>{/if}
        {#if a.info}<p class="ok" role="status">{a.info}</p>{/if}
        <button class="primary" type="submit" disabled={a.busy || code.trim().length < 6}>Confirm</button>
        <div class="row">
          <button type="button" class="linkish" onclick={() => a.pendingEmail && resendCode(a.pendingEmail)} disabled={a.busy}>Send a new code</button>
          <button type="button" class="linkish" onclick={() => { auth.update((s) => ({ ...s, pendingEmail: null, error: null, info: null })); mode = 'signin'; }}>Back</button>
        </div>
      </form>
    {:else}
      {#if completing}
        <h1>One more step</h1>
        <p class="note">Tell us a little about you. It helps us decide what to build next, and it is never shown to anyone else.</p>
      {:else if a.gate === 'reconnect'}
        <h1>Please sign in again</h1>
        <p class="note">radarx has not been able to check your account for more than {OFFLINE_GRACE_DAYS} days. Connect to the internet and sign in; your files and settings are untouched.</p>
      {:else}
        <h1>{mode === 'signup' ? 'Create your free account' : mode === 'signin' ? 'Welcome back' : mode === 'forgot' ? 'Reset your password' : 'Choose a new password'}</h1>
        {#if mode === 'signup'}
          <p class="note">radarx needs an account to run: in the app, in the terminal (<code>rxd</code>) and in Python. It is free, and one account works on every computer you use.</p>
        {/if}
      {/if}

      {#if !completing && mode !== 'forgot' && mode !== 'reset' && OAUTH_PROVIDERS.length > 0}
        <div class="social">
          {#each OAUTH_PROVIDERS as p (p.id)}
            <button type="button" class="prov" disabled={a.busy} onclick={() => signInWith(p.id)}>
              <span class="pmark" data-p={p.id} aria-hidden="true"></span>Continue with {p.label}
            </button>
          {/each}
          {#if a.busy}
            <button type="button" class="linkish" onclick={abortOAuth}>Cancel the browser sign-in</button>
          {/if}
        </div>
        <div class="or"><span>or use e-mail</span></div>
      {/if}

      <form onsubmit={submit} novalidate>
        {#if isSignup || completing}
          <label>
            <span>Your name</span>
            <input autocomplete="name" bind:value={name} />
            {#if touched && problems.name}<small class="err">{problems.name}</small>{/if}
          </label>
        {/if}

        {#if !completing}
          <label>
            <span>E-mail</span>
            <input type="email" autocomplete="email" bind:value={email} />
            {#if touched && problems.email}<small class="err">{problems.email}</small>{/if}
          </label>
          {#if mode === 'reset'}
            <label>
              <span>Code from the e-mail</span>
              <input class="code" inputmode="numeric" autocomplete="one-time-code" maxlength="10" bind:value={code} placeholder="123456" />
              {#if touched && problems.code}<small class="err">{problems.code}</small>{/if}
            </label>
          {/if}
          {#if mode !== 'forgot'}
            <label>
              <span>{mode === 'reset' ? 'New password' : 'Password'}</span>
              <input type="password" autocomplete={isSignup || mode === 'reset' ? 'new-password' : 'current-password'} bind:value={password} />
              {#if touched && problems.password}<small class="err">{problems.password}</small>{/if}
            </label>
          {/if}
        {/if}

        {#if isSignup || completing}
          <label>
            <span>Affiliation</span>
            <input placeholder="University, agency or company" bind:value={affiliation} disabled={independent} autocomplete="organization" />
            {#if touched && problems.affiliation}<small class="err">{problems.affiliation}</small>{/if}
          </label>
          <label class="check">
            <input type="checkbox" bind:checked={independent} />
            <span>I am independent (no affiliation)</span>
          </label>
          <label>
            <span>What will you mostly use radarx for?</span>
            <select bind:value={useCase}>
              {#each USE_CASES as u (u.id)}<option value={u.id}>{u.label}</option>{/each}
            </select>
          </label>
          <label class="check agree">
            <input type="checkbox" bind:checked={agree} />
            <span>
              I accept the
              <button type="button" class="linkish" onclick={() => link(TERMS_URL)}>licence</button>
              and the
              <button type="button" class="linkish" onclick={() => link(PRIVACY_URL)}>privacy notice</button>
              <span class="ver">(v{TERMS_VERSION})</span>
            </span>
          </label>
          {#if touched && problems.agree}<small class="err">{problems.agree}</small>{/if}
          <p class="fine">
            radarx records which <em>features</em> you use (for example "ran an algorithm", "exported a file"), never file names,
            places, radar sites or your data. You can see every entry in <strong>Account &gt; Activity</strong>.
          </p>
        {/if}

        {#if a.error}<p class="err" role="alert">{a.error}</p>{/if}
        {#if a.info}<p class="ok" role="status">{a.info}</p>{/if}

        <button class="primary" type="submit" disabled={a.busy}>
          {completing ? 'Continue' : mode === 'signup' ? 'Create account' : mode === 'signin' ? 'Sign in' : mode === 'forgot' ? 'Send reset code' : 'Set new password'}
        </button>
      </form>

      {#if completing}
        <div class="row"><button type="button" class="linkish" onclick={signOut}>Use a different account</button></div>
      {:else}
        <div class="row">
          {#if mode === 'signup'}
            <button type="button" class="linkish" onclick={() => { mode = 'signin'; touched = false; }}>I already have an account</button>
          {:else}
            <button type="button" class="linkish" onclick={() => { mode = mode === 'reset' ? 'signin' : 'signup'; touched = false; }}>{mode === 'reset' ? 'Back to sign in' : 'Create an account'}</button>
            {#if mode === 'signin'}<button type="button" class="linkish" onclick={() => { mode = 'forgot'; touched = false; }}>Forgot password</button>{/if}
          {/if}
        </div>
      {/if}
    {/if}

    <p class="fine">Your account stores your name, e-mail, affiliation, the computers you sign in from and a feature-level activity log (never file names, places or data). Radar files never leave your machine.</p>
  </div>
</div>

<style>
  .gate {
    position: fixed;
    inset: 0;
    display: grid;
    place-items: center;
    background: radial-gradient(ellipse at 50% 20%, var(--c-surface-2), var(--c-surface-0) 70%);
    overflow-y: auto;
    padding: var(--space-5) 16px;
  }
  .scope { position: absolute; inset: 0; display: grid; place-items: center; opacity: 0.18; pointer-events: none; }
  .card {
    position: relative;
    width: 400px;
    max-width: 100%;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    padding: var(--space-5);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .brand { display: flex; align-items: center; gap: var(--space-3); }
  .name { font-size: var(--fs-lg); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
  .name .x { color: var(--c-accent); }
  .sub { font-size: var(--fs-xs); color: var(--c-muted); }
  h1 { margin: var(--space-2) 0 0; font-size: 18px; font-weight: 600; }
  .note { margin: 0; color: var(--c-text-dim); font-size: var(--fs-sm); line-height: 1.5; }
  form { display: flex; flex-direction: column; gap: var(--space-3); }
  label { display: flex; flex-direction: column; align-items: stretch; text-align: left; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  label.check { flex-direction: row; align-items: flex-start; gap: var(--space-2); }
  label.check input { width: auto; min-height: 0; }
  label.check input { margin-top: 2px; }
  input:not([type='checkbox']), select {
    background: var(--c-field);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    color: var(--c-text);
    padding: 9px 10px;
    font: inherit;
    font-size: var(--fs-md);
    width: 100%;
    box-sizing: border-box;
    height: auto;
    min-height: 38px;
    line-height: 1.3;
  }
  input:focus-visible, select:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 1px; }
  input:disabled { opacity: 0.45; }
  .code { font-family: var(--font-mono); letter-spacing: 0.3em; text-align: center; font-size: 18px; }
  .social { display: flex; flex-direction: column; gap: var(--space-2); }
  .prov {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    padding: 9px 10px;
    background: var(--c-surface-3);
    border: 1px solid var(--c-border-strong);
    color: var(--c-text);
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: var(--fs-md);
  }
  .prov:hover:not(:disabled) { border-color: var(--c-accent); }
  .prov:disabled { opacity: 0.5; cursor: default; }
  .pmark { width: 16px; height: 16px; display: inline-block; border-radius: 3px; background: var(--c-muted); }
  .pmark[data-p='google'] { background: conic-gradient(var(--c-alarm) 0 25%, var(--c-caution) 0 50%, var(--c-nominal) 0 75%, var(--c-accent) 0); border-radius: 50%; }
  .pmark[data-p='apple'] { background: var(--c-text); border-radius: 50% 50% 45% 45%; }
  .pmark[data-p='facebook'] { background: var(--c-accent); border-radius: 50%; }
  .or { display: flex; align-items: center; gap: var(--space-2); color: var(--c-muted); font-size: var(--fs-xs); }
  .or::before, .or::after { content: ''; flex: 1; height: 1px; background: var(--c-border); }
  .row { display: flex; justify-content: space-between; gap: var(--space-3); }
  .linkish { background: none; border: none; padding: 0; color: var(--c-accent); cursor: pointer; font: inherit; font-size: var(--fs-xs); }
  .linkish:hover:not(:disabled) { color: var(--c-accent-strong); text-decoration: underline; }
  .err { color: var(--c-alarm); font-size: var(--fs-xs); margin: 0; }
  .ok { color: var(--c-nominal); font-size: var(--fs-xs); margin: 0; }
  .ver { color: var(--c-muted); }
  .fine { margin: var(--space-1) 0 0; font-size: var(--fs-2xs); color: var(--c-muted); line-height: 1.5; }
  .primary { padding: 10px; }
</style>
