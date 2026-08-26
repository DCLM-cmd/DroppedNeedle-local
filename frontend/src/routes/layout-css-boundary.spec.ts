import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const source = (relativePath: string): string =>
	readFileSync(new URL(relativePath, import.meta.url), 'utf8');

describe('auth-free CSS boundary', () => {
	it('keeps the full stylesheet behind the authenticated shell', () => {
		expect(source('./+layout.svelte')).not.toContain("import '../app.css'");
		expect(source('../lib/components/AuthenticatedAppShell.svelte')).toContain(
			"import '../../app.css'"
		);
	});

	it('uses one explicitly source-limited stylesheet on every auth-free route', () => {
		const authCss = source('../auth.css');
		expect(authCss).toContain("@import 'tailwindcss' source(none)");
		for (const route of ['login', 'setup', 'recover-password']) {
			expect(source(`./${route}/+page.svelte`)).toContain("import '../../auth.css'");
			expect(authCss).toContain(`@source './routes/${route}/+page.svelte'`);
		}
		expect(source('./auth/callback/+page.svelte')).toContain("import '../../../auth.css'");
		expect(authCss).toContain("@source './routes/auth/callback/+page.svelte'");
	});
});
