/*
 * Input-mode decision (docs/DISPLAY.md 12.6): C (hidden, --inject events) when the launched install
 * carries mr/display-input, B (mirror) only as an explicit opt-in, A (view-only) otherwise. Pure.
 */
import { sessionSupportsEvents } from './kwLive';
import type { InjectMode, KwLiveSession } from '../shared/types';

export type ModePreference = 'auto' | 'hidden' | 'mirror' | 'view';

/** Decide the helper's inject mode from the user's preference and what the launched install supports. */
export function resolveInject(pref: ModePreference, session: KwLiveSession | null): { inject: InjectMode; warning?: string } {
  const patched = session ? sessionSupportsEvents(session) : false;
  switch (pref) {
    case 'view': return { inject: 'none' };
    case 'mirror': return { inject: 'mirror' };
    case 'hidden': return patched ? { inject: 'events' } : { inject: 'none', warning: 'This KiCad install does not carry mr/display-input (capability gui.input_from_events); showing a view-only picture. Pick a build with the patch or set kiwrap.live.mode to "mirror".' };
    case 'auto': default: return { inject: patched ? 'events' : 'none' };
  }
}

