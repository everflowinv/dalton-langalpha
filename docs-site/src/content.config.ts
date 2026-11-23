import { defineCollection } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';
import {
  CORE_OVERVIEW_SLUG,
  EVERFLOW_OVERVIEW_SLUG,
  isDocIncluded,
  mapDocSlug,
  resolveDocsProfile,
  rewriteCoreFrontmatterValue,
  transformDocBody,
} from './integrations/profile-filter.mjs';

const { profile, isCore, brandName } = resolveDocsProfile();
const baseLoader = docsLoader();

const profileFilteredDocsLoader = {
  ...baseLoader,
  name: 'dalton-profile-docs-loader',
  async load(context: Parameters<typeof baseLoader.load>[0]) {
    const cacheKey = `${profile}:${brandName}`;
    const prevKey = context.meta?.get('dalton_docs_profile_key');
    if (prevKey !== cacheKey) {
      context.store.clear();
      context.meta?.set('dalton_docs_profile_key', cacheKey);
    }

    const origSet = context.store.set.bind(context.store);
    const filteredStore = new Proxy(context.store, {
      get(target, prop, receiver) {
        if (prop === 'set') {
          return (entry: Parameters<typeof origSet>[0]) => {
            const rel = entry.filePath || entry.id;
            if (!isDocIncluded(rel, { isCore })) {
              context.store.delete(entry.id);
              return false;
            }
            const nextEntry = { ...entry };
            if (isCore && typeof nextEntry.id === 'string') {
              const mappedId = mapDocSlug(nextEntry.id, { isCore });
              if (mappedId !== nextEntry.id) {
                context.store.delete(nextEntry.id);
                nextEntry.id = mappedId;
              }
            }
            if (typeof nextEntry.body === 'string') {
              nextEntry.body = transformDocBody(nextEntry.body, { isCore, filePath: rel });
            }
            if (isCore && nextEntry.data && typeof nextEntry.data === 'object') {
              const data = { ...(nextEntry.data as Record<string, unknown>) };
              if (typeof data.title === 'string') {
                data.title = rewriteCoreFrontmatterValue(data.title, brandName);
              }
              if (typeof data.description === 'string') {
                data.description = rewriteCoreFrontmatterValue(data.description, brandName);
              }
              if (data.hero && typeof data.hero === 'object') {
                const hero = { ...(data.hero as Record<string, unknown>) };
                if (typeof hero.tagline === 'string') {
                  hero.tagline = rewriteCoreFrontmatterValue(hero.tagline, brandName);
                }
                if (Array.isArray(hero.actions)) {
                  hero.actions = hero.actions.map((act) => {
                    if (!act || typeof act !== 'object') return act;
                    const nextAct = { ...(act as Record<string, unknown>) };
                    if (typeof nextAct.link === 'string') {
                      nextAct.link = nextAct.link.replace(
                        `/help/${EVERFLOW_OVERVIEW_SLUG}/`,
                        `/help/${CORE_OVERVIEW_SLUG}/`,
                      );
                    }
                    return nextAct;
                  });
                }
                data.hero = hero;
              }
              nextEntry.data = data;
            }
            return origSet(nextEntry);
          };
        }
        const val = Reflect.get(target, prop, receiver);
        return typeof val === 'function' ? val.bind(target) : val;
      },
    });
    const result = await baseLoader.load({ ...context, store: filteredStore });
    // Defensively purge any stale entry left from a previous profile build.
    for (const [id, entry] of context.store.entries()) {
      const rel = entry?.filePath || id;
      if (!isDocIncluded(rel, { isCore }) || (isCore && id === EVERFLOW_OVERVIEW_SLUG)) {
        context.store.delete(id);
      }
    }
    return result;
  },
};

export const collections = {
  docs: defineCollection({
    loader: profileFilteredDocsLoader,
    schema: docsSchema(),
  }),
};
