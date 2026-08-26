import { describe, expect, it } from 'vitest';
import { getSearchStatusNotice } from './searchStatus';

describe('getSearchStatusNotice', () => {
	it('omits a notice for a healthy provider', () => {
		expect(getSearchStatusNotice('ok', 'artists')).toBeNull();
	});

	it('marks cached results as informational', () => {
		expect(getSearchStatusNotice('stale', 'artists')).toEqual({
			message:
				"MusicBrainz is unavailable, so we're showing cached artist results alongside any matches in your library.",
			className: 'alert-info'
		});
	});

	it('does not claim dedicated pages include local-library matches', () => {
		expect(getSearchStatusNotice('stale', 'artists', false)).toEqual({
			message: "MusicBrainz is unavailable, so we're showing cached artist results.",
			className: 'alert-info'
		});
		expect(getSearchStatusNotice('error', 'albums', false)).toEqual({
			message: 'MusicBrainz album search is temporarily unavailable.',
			className: 'alert-warning'
		});
	});

	it.each([
		['timeout', 'MusicBrainz album search timed out. Any matches in your library are still shown.'],
		[
			'partial',
			'Some MusicBrainz album results could not be loaded. Available results are shown below.'
		],
		[
			'error',
			'MusicBrainz album search is temporarily unavailable. Any matches in your library are still shown.'
		]
	] as const)('uses a warning for %s', (status, message) => {
		expect(getSearchStatusNotice(status, 'albums')).toEqual({
			message,
			className: 'alert-warning'
		});
	});
});
