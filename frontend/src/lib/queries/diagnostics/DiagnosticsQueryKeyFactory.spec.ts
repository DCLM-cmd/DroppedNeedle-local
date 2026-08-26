import { describe, expect, it } from 'vitest';

import { DiagnosticsQueryKeyFactory } from './DiagnosticsQueryKeyFactory';

describe('DiagnosticsQueryKeyFactory', () => {
	it('nests both gauges under an admin/diagnostics parent segment', () => {
		expect(DiagnosticsQueryKeyFactory.queueStats()).toEqual([
			'admin',
			'diagnostics',
			'queue-stats'
		]);
		expect(DiagnosticsQueryKeyFactory.providerStats()).toEqual([
			'admin',
			'diagnostics',
			'provider-stats'
		]);
	});

	it('shares one prefix so an admin-prefix sweep clears both panels', () => {
		const [adminSegment, diagnosticsSegment] = DiagnosticsQueryKeyFactory.prefix;
		expect(adminSegment).toBe('admin');
		expect(diagnosticsSegment).toBe('diagnostics');
		expect(DiagnosticsQueryKeyFactory.queueStats().slice(0, 2)).toEqual(
			DiagnosticsQueryKeyFactory.prefix
		);
		expect(DiagnosticsQueryKeyFactory.providerStats().slice(0, 2)).toEqual(
			DiagnosticsQueryKeyFactory.prefix
		);
	});
});
