import { writable } from 'svelte/store';
import { emptyDraft, type Draft } from '../utils/reportIssue';

/** Is the "Report an issue" dialog open? (Opened from About, the command palette and the Account dialog.) */
export const reportIssueOpen = writable(false);

/**
 * The text the person has typed. It lives here, in memory, so that it survives a failed send, a closed dialog and a
 * reopened one; it is cleared when the report has been sent. Never written to disk.
 */
export const reportDraft = writable<Draft>(emptyDraft());

export function openReportIssue(): void {
  reportIssueOpen.set(true);
}
export function closeReportIssue(): void {
  reportIssueOpen.set(false);
}
export function clearReportDraft(): void {
  reportDraft.set(emptyDraft());
}
