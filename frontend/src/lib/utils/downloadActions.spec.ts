import { beforeEach, describe, expect, it, vi } from 'vitest';

const blob = vi.hoisted(() => ({ download: vi.fn() }));
vi.mock('$lib/utils/blobDownload', () => ({ downloadBlob: blob.download }));
const toast = vi.hoisted(() => ({ show: vi.fn() }));
vi.mock('$lib/stores/toast', () => ({ toastStore: { show: toast.show } }));

import { downloadAlbumArchive, downloadTrackFile } from './downloadActions';

beforeEach(() => {
	vi.clearAllMocks();
	blob.download.mockResolvedValue(undefined);
});

describe('downloadAlbumArchive', () => {
	it('fetches the archive and toasts success', async () => {
		expect.assertions(2);
		await downloadAlbumArchive('/api/v1/download/local/album/mbid/M1');

		expect(blob.download).toHaveBeenCalledWith('/api/v1/download/local/album/mbid/M1');
		expect(toast.show).toHaveBeenCalledWith({
			message: 'Album download started',
			type: 'success'
		});
	});

	it('toasts a user-safe error when the fetch fails', async () => {
		expect.assertions(3);
		blob.download.mockRejectedValueOnce(new Error('gone'));

		await downloadAlbumArchive('/api/v1/download/local/album/A1');

		expect(toast.show).toHaveBeenCalledTimes(1);
		expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ type: 'error' }));
		const message = String(vi.mocked(toast.show).mock.calls[0][0].message);
		expect(message.includes('/api/v1/download')).toBe(false);
	});
});

describe('downloadTrackFile', () => {
	it('fetches the file and toasts success with the title', async () => {
		expect.assertions(2);
		await downloadTrackFile('/api/v1/download/local/track/F1', 'Avalon');

		expect(blob.download).toHaveBeenCalledWith('/api/v1/download/local/track/F1');
		expect(toast.show).toHaveBeenCalledWith({
			message: 'Download started: Avalon',
			type: 'success'
		});
	});

	it('toasts a user-safe error when the fetch fails', async () => {
		expect.assertions(2);
		blob.download.mockRejectedValueOnce(new Error('gone'));

		await downloadTrackFile('/api/v1/download/local/track/F9', 'Avalon');

		expect(toast.show).toHaveBeenCalledTimes(1);
		expect(toast.show).toHaveBeenCalledWith({
			message: 'Could not download Avalon',
			type: 'error'
		});
	});
});
