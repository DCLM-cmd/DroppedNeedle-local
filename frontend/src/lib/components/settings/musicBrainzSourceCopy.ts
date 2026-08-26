/**
 * Copy + pure policy helpers for the MusicBrainz three-way source picker
 * (OWNER DECISION 2026-08-24, mb-localization-phase-2-full-mirror.md).
 * Words here are design material: plain language, honest about tradeoffs,
 * written from the operator's side of the screen. Warnings, never walls.
 */

export type MusicBrainzSourceMode = 'official' | 'mirror' | 'community';

export const MUSICBRAINZ_GUIDE_HREF = '/docs/musicbrainz-mirror-selfhosting.md';

/** Non-official bounds widened by the owner decision; official stays pinned forever. */
export const OFFICIAL_RATE_MAX = 1;
export const OFFICIAL_CONCURRENT_MAX = 6;
export const NON_OFFICIAL_RATE_MAX = 500;
export const NON_OFFICIAL_CONCURRENT_MAX = 64;
/** rate_limit = 0 is the OFF-OFFICIAL sentinel: bypasses the client limiter. */
export const UNLIMITED_RATE_SENTINEL = 0;

export interface SourceBounds {
	rateMax: number;
	concurrentMax: number;
	allowUnlimitedRate: boolean;
}

/** Input max attributes follow the ACTIVE card's bounds; official is clamped, never raised. */
export function sourceBounds(isOfficial: boolean): SourceBounds {
	return isOfficial
		? {
				rateMax: OFFICIAL_RATE_MAX,
				concurrentMax: OFFICIAL_CONCURRENT_MAX,
				allowUnlimitedRate: false
			}
		: {
				rateMax: NON_OFFICIAL_RATE_MAX,
				concurrentMax: NON_OFFICIAL_CONCURRENT_MAX,
				allowUnlimitedRate: true
			};
}

/**
 * The backend's 503 verify branch says "Connected, but rate-limited. Try lowering your
 * rate limit." - advice written for the official endpoint. Off-official, a 503 means the
 * server is busy / its search index is not ready; reword instead of alarming.
 */
export function displayVerifyMessage(message: string, isOfficial: boolean): string {
	if (isOfficial || !/rate-limited/i.test(message)) {
		return message;
	}
	return (
		'Connected, but the server returned 503 (busy). On a mirror this usually means the ' +
		'search index is still building or the service is starting up - it is not a ' +
		'rate-limit problem, so there is nothing to lower.'
	);
}

export interface SourceCardCopy {
	mode: MusicBrainzSourceMode;
	title: string;
	badge: string | null;
	blurb: string;
}

export const MUSICBRAINZ_SOURCE_CARDS: SourceCardCopy[] = [
	{
		mode: 'official',
		title: 'Official',
		badge: 'Recommended',
		blurb: 'The public musicbrainz.org API. Politeness-capped at 1 request per second.'
	},
	{
		mode: 'mirror',
		title: 'Self-hosted mirror',
		badge: null,
		blurb: 'Your own full copy of the MusicBrainz database, on your hardware.'
	},
	{
		mode: 'community',
		title: 'Community / external server',
		badge: null,
		blurb: "Somebody else's server answering MusicBrainz queries for you."
	}
];

export const MORE_INFO_SUMMARY = 'More info';

export const MORE_INFO_DISCLOSURES: Record<MusicBrainzSourceMode, string[]> = {
	official: [
		'Why the 1 request/second cap? musicbrainz.org is one shared public service with a ' +
			'provider-enforced limit. Staying under it is both courtesy to every other consumer ' +
			'and protection against getting your own access throttled.',
		'This is the recommended default: zero setup, always current, and DroppedNeedle caches ' +
			'aggressively so the cap rarely bites.'
	],
	mirror: [
		'Running a mirror means hosting your own full copy of the MusicBrainz database. Be ' +
			'honest with yourself about the footprint: roughly 8-16 GB of RAM and 100-350 GB of ' +
			'disk, plus a weekly search-index rebuild.',
		'Your data is exactly as fresh as your replication schedule, and search quality depends ' +
			'on your reindex cadence - which is precisely why raised or unlimited request limits ' +
			'are fine here. It is your machine; you set the rules.',
		'The setup guide walks through musicbrainz-docker, the replication token, sizing, and ' +
			'the one-line command to check your data vintage.'
	],
	community: [
		"What this is: somebody else's server answers MusicBrainz queries for you. Volunteer-run " +
			'mirror copies catch honest mistakes - they do not protect against deliberately bad ' +
			'data, and one operator controls the front door. Some public dashboards even display ' +
			"other people's search terms.",
		'Protocol caveat: the currently known BrainzMash shared pool speaks a different API ' +
			'dialect and WILL fail the Test Connection below. That is expected, not a bug. Any ' +
			'server genuinely speaking the MusicBrainz ws/2 format plugs straight in.',
		'Politeness: "Unlimited" is meant for your own hardware. Be reasonable with servers ' +
			'you do not own.',
		'Your choice, your ownership: identity decisions made while connected to this server ' +
			'stay fully enabled - nothing is blocked or downgraded. They belong to whoever chose ' +
			'the source: you.'
	]
};

export const COMMUNITY_RISK_BANNER =
	'Community servers are run by volunteers: trust the operator before you trust the data. ' +
	'Quorum-style copies catch accidents, not coordinated bad data; one person controls the ' +
	'front door, and some public dashboards leak search terms.';

export const COMMUNITY_CONFIRM_LABEL =
	'I understand the risks of routing identity data through a server I do not control.';

export const COMMUNITY_CONFIRM_BUTTON_HINT = 'Acknowledge the risk notice to enable saving.';

export const MIRROR_BANNER_LINES = [
	'You are using a non-official MusicBrainz endpoint. Search results depend on that ' +
		"server's reindex schedule - search indexes are not replicated.",
	"Metadata freshness is bounded by the operator's replication cron.",
	'Identity verification quality depends on the operator.'
];

export const CLAMPED_WARNING =
	'Values were clamped to official limits - the public musicbrainz.org endpoint always runs ' +
	'at 1 request/second and 6 concurrent searches regardless of entered values.';

export const UNLIMITED_RATE_LABEL = 'Unlimited';

export const OFFICIAL_ENDPOINT_URL = 'https://musicbrainz.org/ws/2';
