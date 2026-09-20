/*
 * Input-mode decision (docs/DECISIONS.md D20, docs/DISPLAY.md 12.6): the product path is the live
 * view (`none`: the tab is a picture, "Raise KiCad" brings the real window to the front). The
 * injecting modes are EXPERIMENTAL and off by default (setting kiwrap.live.inputMode): `mirror` /
 * `hybrid` drive the visible KiCad from the tab, `events` needs a build carrying mr/display-input
 * (kw_live launches it `hidden` = cloaked) and falls back to the view with a warning otherwise. Pure.
 */
import { sessionSupportsEvents } from './kwLive';
import { INPUT_MODES, type InjectMode, type InputModePreference, type KwLiveSession, type LiveLaunchMode } from '../shared/types';

export type { InputModePreference } from '../shared/types';

/** Coerce a setting value to a known input mode (`none` for anything else). */
export function inputModeOf(value: unknown): InputModePreference {
  return typeof value === 'string' && (INPUT_MODES as readonly string[]).includes(value) ? (value as InputModePreference) : 'none';
}

/** The kw_live launch mode for a preference: only the experimental `events` mode asks for a cloaked KiCad. */
export function launchModeFor(pref: InputModePreference): LiveLaunchMode {
  return pref === 'events' ? 'hidden' : 'view';
}

/** Decide the helper's inject mode from the user's preference and what the launched session supports. */
export function resolveInject(pref: InputModePreference, session: KwLiveSession | null): { inject: InjectMode; warning?: string } {
  switch (pref) {
    case 'mirror': return { inject: 'mirror' };
    case 'hybrid': return { inject: 'hybrid' };
    case 'events':
      return session && sessionSupportsEvents(session)
        ? { inject: 'events' }
        : { inject: 'none', warning: 'kiwrap.live.inputMode is "events" (experimental) but this KiCad did not start cloaked (it needs a build carrying mr/display-input); showing the live view instead.' };
    case 'none': default: return { inject: 'none' };
  }
}
