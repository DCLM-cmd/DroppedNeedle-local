import { beforeEach, describe, expect, it, vi } from 'vitest';

// Mutable fixtures read through getters so each test can vary the SvelteKit
// base and the dynamic public env without re-importing the modules under test.
const mocks = vi.hoisted(() => ({
	base: '',
	publicEnv: {} as Record<string, string>
}));

vi.mock('$app/paths', () => ({
	get base() {
		return mocks.base;
	}
}));

vi.mock('$env/dynamic/public', () => ({
	env: mocks.publicEnv
}));

import { getApiUrl } from './api-utils';
import { withBasePath, withoutBasePath } from '$lib/utils/basePath';

function setBase(value: string): void {
	mocks.base = value;
}

function setPublicApiUrl(value?: string): void {
	for (const key of Object.keys(mocks.publicEnv)) delete mocks.publicEnv[key];
	if (value !== undefined) mocks.publicEnv.PUBLIC_API_URL = value;
}

beforeEach(() => {
	setBase('');
	setPublicApiUrl();
});

describe('withBasePath', () => {
	describe('empty base', () => {
		it('is a byte-compatible identity for root-relative paths', () => {
			expect(withBasePath('/api/v1/auth/me')).toBe('/api/v1/auth/me');
			expect(withBasePath('/api/v1/covers/?size=lg')).toBe('/api/v1/covers/?size=lg');
		});

		it('passes external and pseudo URLs through verbatim', () => {
			expect(withBasePath('https://example.com/a?b=c')).toBe('https://example.com/a?b=c');
			expect(withBasePath('data:image/png;base64,AAA')).toBe('data:image/png;base64,AAA');
			expect(withBasePath('blob:http://localhost/abc')).toBe('blob:http://localhost/abc');
			expect(withBasePath('mailto:someone@example.com')).toBe('mailto:someone@example.com');
		});

		it('passes hash links, protocol-relative URLs, relative paths, and empty strings through', () => {
			expect(withBasePath('#section')).toBe('#section');
			expect(withBasePath('//cdn.example.com/x')).toBe('//cdn.example.com/x');
			expect(withBasePath('assets/song.mp3')).toBe('assets/song.mp3');
			expect(withBasePath('')).toBe('');
		});
	});

	describe("base '/dn'", () => {
		beforeEach(() => setBase('/dn'));

		it('prefixes plain API paths once', () => {
			expect(withBasePath('/api/v1/auth/me')).toBe('/dn/api/v1/auth/me');
		});

		it('preserves trailing slashes, queries, and fragments on transformed paths', () => {
			expect(withBasePath('/api/v1/stream?id=7#t=30')).toBe('/dn/api/v1/stream?id=7#t=30');
			expect(withBasePath('/api/v1/covers/?size=lg')).toBe('/dn/api/v1/covers/?size=lg');
		});

		it('is idempotent for already-prefixed paths', () => {
			const once = withBasePath('/api/v1/artists');
			expect(withBasePath(once)).toBe(once);
		});

		it('collapses duplicated bases back to one', () => {
			expect(withBasePath('/dn/dn/api/v1/x')).toBe('/dn/api/v1/x');
			expect(withBasePath('/dn/dn/dn/api/v1/x')).toBe('/dn/api/v1/x');
		});

		it('still prefixes lookalike first segments', () => {
			expect(withBasePath('/dnextra')).toBe('/dn/dnextra');
			expect(withBasePath('/dnservice/list')).toBe('/dn/dnservice/list');
		});

		it('treats the app root like any other root-relative path', () => {
			expect(withBasePath('/')).toBe('/dn/');
			expect(withBasePath('/dn')).toBe('/dn');
			expect(withBasePath('/dn/')).toBe('/dn');
		});

		it('still prefixes external-style shapes consistently with the contract', () => {
			expect(withBasePath('https://example.com/a')).toBe('https://example.com/a');
			expect(withBasePath('//host/x')).toBe('//host/x');
		});
	});

	describe("multi-segment base '/music/app'", () => {
		beforeEach(() => setBase('/music/app'));

		it('prefixes the full base once', () => {
			expect(withBasePath('/api/v1/artists')).toBe('/music/app/api/v1/artists');
		});

		it('does not treat a partial-overlap first segment as already prefixed', () => {
			expect(withBasePath('/music/x')).toBe('/music/app/music/x');
		});

		it('treats a full-base-string lookalike as a distinct segment', () => {
			// 'application' shares the 'app' bytes but is a different segment.
			expect(withBasePath('/music/application/x')).toBe('/music/app/music/application/x');
		});

		it('is idempotent for fully prefixed paths', () => {
			const once = withBasePath('/playlists');
			expect(withBasePath(once)).toBe(once);
		});
	});
});

describe('withoutBasePath', () => {
	describe('empty base', () => {
		it('is a byte-compatible identity', () => {
			expect(withoutBasePath('/login')).toBe('/login');
			expect(withoutBasePath('/anything?x=1')).toBe('/anything?x=1');
			expect(withoutBasePath('/')).toBe('/');
		});
	});

	describe("base '/dn'", () => {
		beforeEach(() => setBase('/dn'));

		it('strips exactly one leading base from based pathnames', () => {
			expect(withoutBasePath('/dn/login')).toBe('/login');
			expect(withoutBasePath('/dn/api/v1/auth/me')).toBe('/api/v1/auth/me');
		});

		it('maps the bare base to the app root', () => {
			expect(withoutBasePath('/dn')).toBe('/');
			expect(withoutBasePath('/dn/')).toBe('/');
		});

		it('removes only one occurrence, never more', () => {
			expect(withoutBasePath('/dn/dn/login')).toBe('/dn/login');
		});

		it('leaves lookalike first segments untouched', () => {
			expect(withoutBasePath('/dnextra/a')).toBe('/dnextra/a');
			expect(withoutBasePath('/dnservice/list')).toBe('/dnservice/list');
		});

		it('returns non-based paths unchanged', () => {
			expect(withoutBasePath('/api/v1/me')).toBe('/api/v1/me');
			expect(withoutBasePath('/other/deploy/login')).toBe('/other/deploy/login');
		});

		it('rides query strings and fragments through after stripping', () => {
			expect(withoutBasePath('/dn/login?next=%2Fartists#top')).toBe('/login?next=%2Fartists#top');
		});

		it('inverts withBasePath for representative route paths', () => {
			for (const p of ['/', '/artists/42', '/requests', '/settings/appearance?tab=ui']) {
				expect(withoutBasePath(withBasePath(p))).toBe(p);
			}
		});

		it('round-trips lookalike paths that had to be freshly prefixed', () => {
			expect(withoutBasePath(withBasePath('/dnly/reports'))).toBe('/dnly/reports');
		});
	});

	describe("multi-segment base '/music/app'", () => {
		beforeEach(() => setBase('/music/app'));

		it('strips the full multi-segment base', () => {
			expect(withoutBasePath('/music/app/playlists')).toBe('/playlists');
		});

		it('maps the bare full base to the app root', () => {
			expect(withoutBasePath('/music/app')).toBe('/');
		});

		it('keeps lower-segment overlaps verbatim', () => {
			expect(withoutBasePath('/music/playlists')).toBe('/music/playlists');
			expect(withoutBasePath('/music/application/x')).toBe('/music/application/x');
		});

		it('strips only one occurrence of the full base', () => {
			expect(withoutBasePath('/music/app/music/app/settings')).toBe('/music/app/settings');
		});
	});

	describe('passthroughs', () => {
		it('returns external, protocol-relative, relative, hash, and empty inputs verbatim regardless of base', () => {
			setBase('/dn');
			expect(withoutBasePath('https://example.com/dn/x')).toBe('https://example.com/dn/x');
			expect(withoutBasePath('//cdn.example.com/dn')).toBe('//cdn.example.com/dn');
			expect(withoutBasePath('rel/path')).toBe('rel/path');
			expect(withoutBasePath('#hash')).toBe('#hash');
			expect(withoutBasePath('')).toBe('');
		});
	});
});

describe('getApiUrl', () => {
	describe('PUBLIC_API_URL unset', () => {
		it('returns root-relative API paths as-is with an empty base', () => {
			expect(getApiUrl('/api/v1/auth/me')).toBe('/api/v1/auth/me');
		});

		it('composes only the base when no PUBLIC_API_URL origin is configured', () => {
			setBase('/dn');
			expect(getApiUrl('/api/v1/auth/me')).toBe('/dn/api/v1/auth/me');
		});

		it('leaves external protocol-relative URLs byte-for-byte even with a base', () => {
			setBase('/dn');
			expect(getApiUrl('//cdn.example/covers/x.jpg')).toBe('//cdn.example/covers/x.jpg');
		});
	});

	describe('PUBLIC_API_URL set', () => {
		it('prepends the origin alone when the base is empty', () => {
			setPublicApiUrl('http://localhost:8688');
			expect(getApiUrl('/api/v1/auth/me')).toBe('http://localhost:8688/api/v1/auth/me');
		});

		it('trims all trailing slashes from the origin before joining', () => {
			setPublicApiUrl('http://localhost:8688///');
			expect(getApiUrl('/api/v1/auth/me')).toBe('http://localhost:8688/api/v1/auth/me');
		});

		it('composes origin -> base -> path in order', () => {
			setBase('/dn');
			setPublicApiUrl('http://localhost:8688/');
			expect(getApiUrl('/api/v1/auth/me')).toBe('http://localhost:8688/dn/api/v1/auth/me');
		});

		it('ignores absolute URL inputs even when PUBLIC_API_URL is set', () => {
			setPublicApiUrl('http://localhost:8688');
			expect(getApiUrl('https://cdn.example/img.png')).toBe('https://cdn.example/img.png');
		});

		it('leaves external protocol-relative URLs byte-for-byte instead of joining the origin', () => {
			setPublicApiUrl('http://localhost:8688');
			expect(getApiUrl('//cdn.example/covers/x.jpg')).toBe('//cdn.example/covers/x.jpg');
		});

		it('passes data:, blob:, and hash shapes through untouched', () => {
			setPublicApiUrl('http://localhost:8688');
			expect(getApiUrl('data:image/png;base64,AAA')).toBe('data:image/png;base64,AAA');
			expect(getApiUrl('blob:http://localhost/abc')).toBe('blob:http://localhost/abc');
			expect(getApiUrl('#frag')).toBe('#frag');
		});
	});

	describe('double-application safety through the composed result', () => {
		it('stays stable when PUBLIC_API_URL is set because the composed URL is absolute', () => {
			setBase('/dn');
			setPublicApiUrl('http://localhost:8688');
			const once = getApiUrl('/api/v1/auth/me');
			expect(getApiUrl(once)).toBe(once);
		});

		it('collapses a duplicate base when no origin is set', () => {
			setBase('/dn');
			const once = getApiUrl('/api/v1/auth/me');
			expect(getApiUrl(once)).toBe(once);
		});
	});
});
