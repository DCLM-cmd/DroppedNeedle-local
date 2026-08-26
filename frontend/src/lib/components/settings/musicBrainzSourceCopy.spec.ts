import { describe, expect, it } from 'vitest';

import {
	COMMUNITY_CONFIRM_LABEL,
	COMMUNITY_RISK_BANNER,
	displayVerifyMessage,
	MIRROR_BANNER_LINES,
	MORE_INFO_DISCLOSURES,
	MUSICBRAINZ_GUIDE_HREF,
	MUSICBRAINZ_SOURCE_CARDS,
	NON_OFFICIAL_CONCURRENT_MAX,
	NON_OFFICIAL_RATE_MAX,
	OFFICIAL_CONCURRENT_MAX,
	OFFICIAL_RATE_MAX,
	UNLIMITED_RATE_SENTINEL,
	sourceBounds
} from './musicBrainzSourceCopy';

const OFFICIAL_503_MESSAGE = 'Connected, but rate-limited. Try lowering your rate limit.';

describe('MusicBrainz source bounds (owner-widened non-official, pinned official)', () => {
	it('official card stays pinned at 1 r/s / 6 concurrent with no unlimited sentinel', () => {
		expect(sourceBounds(true)).toEqual({
			rateMax: OFFICIAL_RATE_MAX,
			concurrentMax: OFFICIAL_CONCURRENT_MAX,
			allowUnlimitedRate: false
		});
		expect(OFFICIAL_RATE_MAX).toBe(1);
		expect(OFFICIAL_CONCURRENT_MAX).toBe(6);
	});

	it('non-official cards allow the widened bounds plus the unlimited sentinel', () => {
		expect(sourceBounds(false)).toEqual({
			rateMax: NON_OFFICIAL_RATE_MAX,
			concurrentMax: NON_OFFICIAL_CONCURRENT_MAX,
			allowUnlimitedRate: true
		});
		expect(NON_OFFICIAL_RATE_MAX).toBe(500);
		expect(NON_OFFICIAL_CONCURRENT_MAX).toBe(64);
		expect(UNLIMITED_RATE_SENTINEL).toBe(0);
	});
});

describe('verify-result copy remap', () => {
	it('keeps the official-endpoint wording untouched for the official host', () => {
		expect(displayVerifyMessage(OFFICIAL_503_MESSAGE, true)).toBe(OFFICIAL_503_MESSAGE);
	});

	it('rewrites the 503 rate-limited advice for non-official servers', () => {
		const remapped = displayVerifyMessage(OFFICIAL_503_MESSAGE, false);
		expect(remapped).toContain('503');
		expect(remapped).toContain('not a rate-limit problem');
		expect(remapped).not.toContain('Try lowering');
	});

	it('leaves unrelated messages alone on both hosts', () => {
		const message = 'Could not connect to the specified endpoint';
		expect(displayVerifyMessage(message, true)).toBe(message);
		expect(displayVerifyMessage(message, false)).toBe(message);
	});
});

describe('owner-decision copy content', () => {
	it('exposes exactly the three source cards with Official marked recommended', () => {
		expect(MUSICBRAINZ_SOURCE_CARDS.map((card) => card.mode)).toEqual([
			'official',
			'mirror',
			'community'
		]);
		expect(MUSICBRAINZ_SOURCE_CARDS[0].badge).toBe('Recommended');
		expect(MUSICBRAINZ_SOURCE_CARDS.slice(1).every((card) => card.badge === null)).toBe(true);
	});

	it('official disclosure explains why the 1 req/s cap exists', () => {
		const text = MORE_INFO_DISCLOSURES.official.join(' ');
		expect(text).toMatch(/provider-enforced limit/);
		expect(text).toMatch(/1 request\/second/);
	});

	it('mirror disclosure is honest about footprint and freshness ownership', () => {
		const text = MORE_INFO_DISCLOSURES.mirror.join(' ');
		expect(text).toContain('8-16 GB');
		expect(text).toContain('100-350 GB');
		expect(text).toContain('replication schedule');
		expect(text).toContain('reindex');
		expect(text).toContain('your machine');
	});

	it('community disclosure carries the trust story, protocol caveat, politeness, ownership', () => {
		const text = MORE_INFO_DISCLOSURES.community.join(' ');
		expect(text).toContain('BrainzMash');
		expect(text).toContain('WILL fail');
		expect(text).toContain('Test Connection');
		expect(text).toMatch(/not a bug/);
		expect(text).toMatch(/Be reasonable with servers/);
		expect(text).toMatch(/stay fully enabled/);
		expect(COMMUNITY_RISK_BANNER).toMatch(/front door/);
		expect(COMMUNITY_RISK_BANNER).toMatch(/search terms/);
		expect(COMMUNITY_CONFIRM_LABEL).toMatch(/I understand the risks/);
	});

	it('mirror banner lines and guide link follow the plan', () => {
		const joined = MIRROR_BANNER_LINES.join(' ');
		expect(joined).toMatch(/reindex schedule/);
		expect(joined).toMatch(/replication cron/);
		expect(joined).toMatch(/depends on the operator/);
		expect(MUSICBRAINZ_GUIDE_HREF).toBe('/docs/musicbrainz-mirror-selfhosting.md');
	});
});
