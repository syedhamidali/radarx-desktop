/**
 * English messages (the source language: every other catalogue is checked against these keys).
 *
 * Keys are `<component>.<what>`, values are the text a person reads. `{name}` is replaced by a
 * parameter; a message with a count has `_one` and `_other` forms (see ../index.ts). Only the
 * strings of RemoteOpenDialog.svelte are here so far (docs/ACCESSIBILITY.md has the plan).
 */
export const en = {
  'remoteOpen.title': 'Open a file on the remote engine',
  'remoteOpen.leadBefore': 'Type the path of the file',
  'remoteOpen.leadWhere': 'on the computer where rxd serve runs',
  'remoteOpen.reachedAt': '(reached at {hostPort})',
  'remoteOpen.leadAfter': 'The file picker of this computer cannot see it.',
  'remoteOpen.pathLabel': 'Path on the remote computer',
  'remoteOpen.cancel': 'Cancel',
  'remoteOpen.open': 'Open',
} as const;

export type MessageKey = keyof typeof en;
