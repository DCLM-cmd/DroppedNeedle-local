import { describe, it, expect } from 'vitest';
import { formatBytes, formatListenCount } from './formatting';

describe('formatListenCount', () => {
	it('returns empty string for null', () => {
		expect.assertions(1);
		expect(formatListenCount(null)).toBe('');
	});

	it('formats zero as "0 plays"', () => {
		expect.assertions(1);
		expect(formatListenCount(0)).toBe('0 plays');
	});

	it('formats zero compact as "0"', () => {
		expect.assertions(1);
		expect(formatListenCount(0, true)).toBe('0');
	});

	it('formats small number with suffix', () => {
		expect.assertions(1);
		expect(formatListenCount(42)).toBe('42 plays');
	});

	it('formats thousands', () => {
		expect.assertions(1);
		expect(formatListenCount(1500)).toBe('1.5K plays');
	});

	it('formats thousands compact', () => {
		expect.assertions(1);
		expect(formatListenCount(1500, true)).toBe('1.5K');
	});

	it('formats millions', () => {
		expect.assertions(1);
		expect(formatListenCount(2500000)).toBe('2.5M plays');
	});

	it('formats millions compact', () => {
		expect.assertions(1);
		expect(formatListenCount(2500000, true)).toBe('2.5M');
	});

	it('formats billions', () => {
		expect.assertions(1);
		expect(formatListenCount(3596400000)).toBe('3.6B plays');
	});

	it('formats billions compact', () => {
		expect.assertions(1);
		expect(formatListenCount(3596400000, true)).toBe('3.6B');
	});
});

describe('formatBytes', () => {
	it('returns "0 B" for zero, null, and undefined (callers map these to "—")', () => {
		expect.assertions(3);
		expect(formatBytes(0)).toBe('0 B');
		expect(formatBytes(null)).toBe('0 B');
		expect(formatBytes(undefined)).toBe('0 B');
	});

	it('formats byte counts with one decimal below 10 units', () => {
		expect.assertions(2);
		expect(formatBytes(10485760)).toBe('10 MB');
		expect(formatBytes(15728640)).toBe('15 MB');
	});
});
