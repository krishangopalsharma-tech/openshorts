import { getApiUrl } from '../config';

export const LAST_DIR_KEY = 'os_browse_last_dir';
export const RECENTS_KEY = 'os_browse_recents';
export const SORT_KEY = 'os_browse_sort';
export const MAX_RECENTS = 6;

export const readJson = (key, fallback) => {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
};

export const writeStorage = (key, value) => {
  try {
    localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
  } catch {
    /* private mode */
  }
};

/** Remember a folder as recently used. Most recent first, deduped. */
export function rememberFolder(dir) {
  if (!dir) return;
  const next = [dir, ...readJson(RECENTS_KEY, []).filter((d) => d !== dir)];
  writeStorage(RECENTS_KEY, next.slice(0, MAX_RECENTS));
  writeStorage(LAST_DIR_KEY, dir);
}

export function getRecents() {
  return readJson(RECENTS_KEY, []);
}

export const lastFolder = () => {
  try {
    return localStorage.getItem(LAST_DIR_KEY) || '';
  } catch {
    return '';
  }
};

/** The thumbnail URL for a path, for callers that want their own preview. */
export const thumbUrl = (path) =>
  getApiUrl(`/api/local/thumb?path=${encodeURIComponent(path)}`);
