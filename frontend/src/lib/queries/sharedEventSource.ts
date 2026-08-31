/**
 * One EventSource per URL, shared by every subscriber.
 *
 * Browsers cap HTTP/1.1 at ~6 concurrent connections per origin, and an open SSE
 * stream holds one for as long as it lives. The app shell already keeps several
 * streams open on every page, so a component opening its OWN connection to a URL
 * the shell is also listening to spends a second slot on identical data - and once
 * the six are gone, ordinary API requests queue in the browser indefinitely, which
 * looks exactly like the server hanging.
 *
 * Subscribers are ref-counted: the underlying connection opens on the first
 * subscribe and closes when the last one goes away.
 */

type Listener = (event: MessageEvent) => void;

interface SharedConnection {
	source: EventSource;
	/** listeners per event name, so we attach ONE handler per name to the source */
	listeners: Map<string, Set<Listener>>;
	/** attached fan-out handlers, kept so they can be removed on teardown */
	fanout: Map<string, Listener>;
	refs: number;
}

const connections = new Map<string, SharedConnection>();

export interface SharedEventSourceSubscription {
	/** Release this subscriber; closes the connection when it was the last one. */
	close(): void;
}

/**
 * Subscribe to `url`, reusing an existing connection when one is already open.
 *
 * `handlers` maps event name to callback. Use the `'open'` key for the open event.
 */
export function subscribeShared(
	url: string,
	handlers: Record<string, Listener>
): SharedEventSourceSubscription {
	let connection = connections.get(url);
	if (!connection) {
		connection = {
			source: new EventSource(url),
			listeners: new Map(),
			fanout: new Map(),
			refs: 0
		};
		connections.set(url, connection);
	}
	connection.refs += 1;
	const shared = connection;

	const attached: Array<[string, Listener]> = [];
	for (const [name, handler] of Object.entries(handlers)) {
		let listeners = shared.listeners.get(name);
		if (!listeners) {
			listeners = new Set();
			shared.listeners.set(name, listeners);
			// One real listener per event name; it fans out to the subscribers. A
			// late subscriber therefore costs nothing on the wire.
			const fanout: Listener = (event) => {
				for (const listener of shared.listeners.get(name) ?? []) {
					listener(event);
				}
			};
			shared.fanout.set(name, fanout);
			shared.source.addEventListener(name, fanout as EventListener);
		}
		listeners.add(handler);
		attached.push([name, handler]);
	}

	let released = false;
	return {
		close(): void {
			if (released) return;
			released = true;
			for (const [name, handler] of attached) {
				const listeners = shared.listeners.get(name);
				listeners?.delete(handler);
				if (listeners && listeners.size === 0) {
					const fanout = shared.fanout.get(name);
					if (fanout) {
						shared.source.removeEventListener(name, fanout as EventListener);
					}
					shared.listeners.delete(name);
					shared.fanout.delete(name);
				}
			}
			shared.refs -= 1;
			if (shared.refs <= 0) {
				shared.source.close();
				connections.delete(url);
			}
		}
	};
}

/** Test/teardown helper: drop every shared connection. */
export function closeAllSharedEventSources(): void {
	for (const connection of connections.values()) {
		connection.source.close();
	}
	connections.clear();
}
