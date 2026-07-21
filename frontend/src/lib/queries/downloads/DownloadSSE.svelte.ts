import { API } from '$lib/constants';
import type { DownloadProgress } from '$lib/types';

interface DownloadStreamState {
	progress: DownloadProgress | null;
	status: string | null;
	done: boolean;
}

function parse(event: Event): Record<string, unknown> {
	try {
		return JSON.parse((event as MessageEvent).data) as Record<string, unknown>;
	} catch {
		return {};
	}
}

// ONE shared EventSource multiplexes every download's events (demuxed by task_id).
// Browsers cap concurrent HTTP/1.1 connections per origin (~6), so the previous
// one-EventSource-per-task design starved all other requests while several
// downloads were active - the whole app appeared to hang whenever something loaded.
type Handler = (event: string, data: Record<string, unknown>) => void;
const handlers = new Map<string, Set<Handler>>();
let shared: EventSource | null = null;

function dispatch(eventName: string) {
	return (e: Event) => {
		const data = parse(e);
		const taskId = data.task_id as string | undefined;
		if (!taskId) return;
		const set = handlers.get(taskId);
		if (!set) return;
		for (const handler of set) handler(eventName, data);
	};
}

// EventSource authenticates via the droppedneedle_session cookie (no custom headers).
// no 'error' handler so keepalive gaps/close don't clobber a terminal state
function ensureShared(): void {
	if (shared) return;
	shared = new EventSource(API.downloads.streamAll());
	shared.addEventListener('status', dispatch('status'));
	shared.addEventListener('progress', dispatch('progress'));
	shared.addEventListener('complete', dispatch('complete'));
}

function subscribe(taskId: string, handler: Handler): () => void {
	let set = handlers.get(taskId);
	if (!set) {
		set = new Set();
		handlers.set(taskId, set);
	}
	set.add(handler);
	ensureShared();
	return () => {
		const current = handlers.get(taskId);
		if (!current) return;
		current.delete(handler);
		if (current.size === 0) handlers.delete(taskId);
		if (handlers.size === 0 && shared) {
			shared.close();
			shared = null;
		}
	};
}

export function createDownloadStream() {
	let state = $state<DownloadStreamState>({ progress: null, status: null, done: false });
	let unsubscribe: (() => void) | null = null;

	function stop() {
		if (unsubscribe) {
			unsubscribe();
			unsubscribe = null;
		}
	}

	function start(taskId: string) {
		stop();
		state = { progress: null, status: null, done: false };
		unsubscribe = subscribe(taskId, (event, d) => {
			if (event === 'status') {
				state = { ...state, status: (d.status as string) ?? state.status };
			} else if (event === 'progress') {
				state = {
					...state,
					progress: {
						bytes_downloaded: Number(d.bytes_downloaded ?? 0),
						bytes_total: Number(d.bytes_total ?? 0),
						files_completed: Number(d.files_completed ?? 0),
						files_total: Number(d.files_total ?? 0),
						progress_percent: Number(d.progress_percent ?? 0)
					}
				};
			} else if (event === 'complete') {
				state = { ...state, status: (d.status as string) ?? state.status, done: true };
				stop();
			}
		});
	}

	return {
		get state() {
			return state;
		},
		start,
		stop
	};
}
