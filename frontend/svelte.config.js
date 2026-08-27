import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// The Docker image builds with DROPPEDNEEDLE_BASE_PATH_PLACEHOLDER=1, baking
// every root-relative URL behind this literal token so any BASE_PATH can be
// stamped in at container startup (see backend/maintenance/
// configure_frontend_base.py). Local dev builds keep '' and ship no token.
const BASE_PATH_PLACEHOLDER = '/__DROPPEDNEEDLE_BASE__';
/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),

	kit: {
		adapter: adapter({
			pages: 'build',
			assets: 'build',
			fallback: 'index.html',
			precompress: true
		}),
		paths: {
			base: process.env.DROPPEDNEEDLE_BASE_PATH_PLACEHOLDER === '1' ? BASE_PATH_PLACEHOLDER : ''
		},
		appDir: '_app'
	}
};

export default config;
