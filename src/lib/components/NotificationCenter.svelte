<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import { writable, derived, get } from 'svelte/store';
  import { fade, fly } from 'svelte/transition';
  import { toasts, type Toast } from '../stores/toasts';

  export let open = false;

  const dispatch = createEventDispatcher();
  const MAX_ENTRIES = 50;

  interface Notification {
    id: number;
    level: Toast['level'];
    message: string;
    timestamp: number;
    read: boolean;
  }

  let notifications: Notification[] = [];
  let nextId = 1;

  // Track unread count
  $: unreadCount = notifications.filter((n) => !n.read).length;

  // Subscribe to toasts and capture each one exactly once. Toast ids are unique and increasing,
  // so remembering the highest id seen is enough: every store update re-delivers the toasts that
  // are still on screen, and those must not become new notifications.
  let lastToastId = 0;
  const unsubscribe = toasts.subscribe((currentToasts) => {
    const fresh = currentToasts.filter((t) => t.id > lastToastId);
    if (fresh.length === 0) return;
    lastToastId = Math.max(lastToastId, ...fresh.map((t) => t.id));
    notifications = [
      ...fresh
        .map((toast) => ({
          id: nextId++,
          level: toast.level,
          message: toast.message,
          timestamp: Date.now(),
          read: open, // arrived while the panel is open: already seen
        }))
        .reverse(),
      ...notifications,
    ].slice(0, MAX_ENTRIES);
  });

  onDestroy(() => {
    unsubscribe();
  });

  export function addNotification(level: Toast['level'], message: string) {
    notifications = [
      {
        id: nextId++,
        level,
        message,
        timestamp: Date.now(),
        read: false,
      },
      ...notifications,
    ].slice(0, MAX_ENTRIES);
  }

  function toggle() {
    open = !open;
    if (open) {
      markAllRead();
    }
    dispatch(open ? 'open' : 'close');
  }

  function markAllRead() {
    notifications = notifications.map((n) => ({ ...n, read: true }));
  }

  function clearAll() {
    notifications = [];
  }

  function removeNotification(id: number) {
    notifications = notifications.filter((n) => n.id !== id);
  }

  function formatTime(ts: number): string {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function formatRelative(ts: number): string {
    const diff = Date.now() - ts;
    const secs = Math.floor(diff / 1000);
    if (secs < 60) return 'just now';
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ago`;
  }

  function levelIcon(level: string): string {
    switch (level) {
      case 'success': return '\u2713';
      case 'warning': return '\u26A0';
      case 'error': return '\u2717';
      default: return '\u2139';
    }
  }

  function handleClickOutside(e: MouseEvent) {
    if (!open) return;
    const target = e.target as HTMLElement;
    if (!target.closest('.notification-center') && !target.closest('.notif-bell-btn')) {
      open = false;
      dispatch('close');
    }
  }

  function handleKeydown(e: KeyboardEvent) {
    if (open && e.key === 'Escape') {
      open = false;
      dispatch('close');
    }
  }

  onMount(() => {
    document.addEventListener('mousedown', handleClickOutside);
    window.addEventListener('keydown', handleKeydown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      window.removeEventListener('keydown', handleKeydown);
    };
  });
</script>

<div class="notif-container">
  <button class="notif-bell-btn" on:click={toggle} title="Notifications" aria-label="Notifications{unreadCount > 0 ? `, ${unreadCount} unread` : ''}" aria-expanded={open}>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
    {#if unreadCount > 0}
      <span class="notif-badge" transition:fade={{ duration: 100 }}>
        {unreadCount > 9 ? '9+' : unreadCount}
      </span>
    {/if}
  </button>

  {#if open}
    <div class="notification-center" role="region" aria-label="Notifications" transition:fly={{ x: 20, duration: 200 }}>
      <div class="notif-header">
        <h3>Notifications</h3>
        <div class="notif-header-actions">
          {#if notifications.length > 0}
            <button class="notif-action-btn" on:click={clearAll} title="Clear all">
              Clear all
            </button>
          {/if}
          <button class="notif-close-btn" aria-label="Close notifications" on:click={() => { open = false; dispatch('close'); }} title="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="notif-list">
        {#if notifications.length === 0}
          <div class="notif-empty">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
            <span>No notifications yet</span>
          </div>
        {:else}
          {#each notifications as notif (notif.id)}
            <div
              class="notif-item"
              class:unread={!notif.read}
              transition:fly={{ x: 20, duration: 150 }}
            >
              <span class="notif-icon notif-icon-{notif.level}">{levelIcon(notif.level)}</span>
              <div class="notif-content">
                <span class="notif-message">{notif.message}</span>
                <span class="notif-time" title={formatTime(notif.timestamp)}>{formatRelative(notif.timestamp)}</span>
              </div>
              <button class="notif-dismiss" on:click={() => removeNotification(notif.id)} title="Dismiss">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              </button>
            </div>
          {/each}
        {/if}
      </div>

      {#if notifications.length > 0}
        <div class="notif-footer">
          <span class="notif-count">{notifications.length} notification{notifications.length !== 1 ? 's' : ''}</span>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .notif-container {
    position: relative;
  }

  .notif-bell-btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid var(--glass-border);
    color: var(--text-secondary);
    padding: var(--spacing-xs);
    height: 28px;
    width: 28px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
  }

  .notif-bell-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--border-light);
  }

  .notif-badge {
    position: absolute;
    top: -4px;
    right: -4px;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    font-size: 9px;
    font-weight: 700;
    color: var(--c-on-accent);
    background: var(--accent-danger);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    box-shadow: 0 0 6px rgba(var(--c-alarm-rgb), 0.4);
  }

  .notification-center {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    width: 360px;
    max-height: 480px;
    background: var(--overlay-bg);
    backdrop-filter: blur(40px);
    -webkit-backdrop-filter: blur(40px);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg), 0 0 40px rgba(0, 0, 0, 0.3);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    z-index: 100;
  }

  .notif-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid var(--glass-border);
  }

  .notif-header h3 {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
  }

  .notif-header-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
  }

  .notif-action-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-size: 10px;
    padding: 2px 6px;
    cursor: pointer;
    height: auto;
    border-radius: var(--radius-sm);
  }

  .notif-action-btn:hover {
    color: var(--accent-primary);
    background: var(--bg-hover);
  }

  .notif-close-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 2px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
  }

  .notif-close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .notif-list {
    flex: 1;
    overflow-y: auto;
    max-height: 380px;
  }

  .notif-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-2xl);
    color: var(--text-muted);
    font-size: 12px;
  }

  .notif-item {
    display: flex;
    align-items: flex-start;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    border-bottom: 1px solid rgba(var(--c-line-rgb), 0.05);
    transition: background var(--transition-fast);
  }

  .notif-item:hover {
    background: var(--bg-hover);
  }

  .notif-item.unread {
    background: rgba(var(--c-accent-rgb), 0.04);
    border-left: 2px solid var(--accent-primary);
  }

  .notif-icon {
    flex-shrink: 0;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    border-radius: 50%;
    margin-top: 1px;
  }

  .notif-icon-info {
    color: var(--accent-primary);
    background: rgba(var(--c-accent-rgb), 0.12);
  }

  .notif-icon-success {
    color: var(--accent-success);
    background: rgba(var(--c-nominal-rgb), 0.12);
  }

  .notif-icon-warning {
    color: var(--accent-warning);
    background: rgba(var(--c-caution-rgb), 0.12);
  }

  .notif-icon-error {
    color: var(--accent-danger);
    background: rgba(var(--c-alarm-rgb), 0.12);
  }

  .notif-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .notif-message {
    font-size: 12px;
    color: var(--text-primary);
    line-height: 1.4;
    word-break: break-word;
  }

  .notif-time {
    font-size: 10px;
    color: var(--text-muted);
  }

  .notif-dismiss {
    flex-shrink: 0;
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 2px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  .notif-item:hover .notif-dismiss {
    opacity: 1;
  }

  .notif-dismiss:hover {
    color: var(--text-primary);
    background: var(--bg-active);
  }

  .notif-footer {
    padding: var(--spacing-xs) var(--spacing-md);
    border-top: 1px solid var(--glass-border);
    text-align: center;
  }

  .notif-count {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
