import { describe, expect, it } from 'vitest';
import { editionLabel } from './albumEditionLabel';
import type { AlbumEditionItem } from '$lib/types';

function edition(overrides: Partial<AlbumEditionItem> = {}): AlbumEditionItem {
	return {
		release_mbid: '8feb9fef-fad0-4271-9d2b-d9acca84b167',
		track_count: 9,
		title: null,
		disambiguation: null,
		date: null,
		country: null,
		packaging: null,
		status: null,
		is_owned: false,
		is_pinned: false,
		...overrides
	};
}

describe('editionLabel', () => {
	it('shows the release title when it diverges from the album title (Side A/B pair)', () => {
		// the DISSY "morgen werde ich mich dafür hassen" EP: two releases, identical
		// country/track-count, distinguishable only by which songs they contain -
		// the title is the only field that actually tells them apart
		const sideB = edition({
			release_mbid: '53263f4f-b0c2-4faa-ae51-c13269998d1d',
			title: 'morgen werde ich mich dafür hassen (side b)',
			date: '2026-03-27',
			country: 'XW'
		});

		expect(editionLabel(sideB, 'morgen werde ich mich dafür hassen')).toBe(
			'morgen werde ich mich dafür hassen (side b) · 2026 · XW · 9 tracks'
		);
	});

	it('omits the title when it matches the album title, to avoid repeating it verbatim', () => {
		const main = edition({ title: 'morgen werde ich mich dafür hassen', date: '2025-06-20', country: 'XW' });

		expect(editionLabel(main, 'morgen werde ich mich dafür hassen')).toBe('2025 · XW · 9 tracks');
	});

	it('title comparison is case- and whitespace-insensitive', () => {
		const e = edition({ title: '  Morgen Werde Ich Mich Dafür Hassen  ' });

		expect(editionLabel(e, 'morgen werde ich mich dafür hassen')).toBe('9 tracks');
	});

	it('two same-format editions with different titles produce distinguishable labels', () => {
		// the actual regression: before this fix, both of DISSY's releases showed as
		// "2025 · XW · 9 tracks" / "2026 · XW · 9 tracks" with no titles at all - only
		// the year told them apart, and neither hinted at "side b"
		const main = edition({ title: 'morgen werde ich mich dafür hassen', date: '2025-06-20', country: 'XW' });
		const sideB = edition({
			title: 'morgen werde ich mich dafür hassen (side b)',
			date: '2026-03-27',
			country: 'XW'
		});

		const albumTitle = 'morgen werde ich mich dafür hassen';
		expect(editionLabel(main, albumTitle)).not.toBe(editionLabel(sideB, albumTitle));
		expect(editionLabel(sideB, albumTitle)).toContain('side b');
	});
});
