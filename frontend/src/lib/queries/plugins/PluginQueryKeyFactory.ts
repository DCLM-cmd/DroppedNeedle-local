// Plugin list/config is global admin state, not user-dependent, so no userId segment.
// Sources feed per-user acquisition surfaces (priority/review labels), so
// sources()/ui() carry userId like the downloads keys.
export const PluginQueryKeyFactory = {
	prefix: ['plugins'] as const,
	list: () => [...PluginQueryKeyFactory.prefix, 'list'] as const,
	sources: (userId?: string) =>
		[...PluginQueryKeyFactory.prefix, 'sources', userId ?? 'anon'] as const,
	ui: (userId: string | undefined, name: string) =>
		[...PluginQueryKeyFactory.prefix, 'ui', userId ?? 'anon', name] as const
};
