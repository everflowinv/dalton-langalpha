/**
 * The gallery's horizontal frame, shared by every band of it (the header, the
 * banner, the Chief of Staff card and toolbar, and the grid) so they all start
 * at one left edge and end at one right edge.
 *
 * From a laptop's width up, only the grid scrolls: each band spans the whole
 * width beside the sidebar and centres its column inside, so the grid's
 * scrollbar sits at the window's edge rather than beside the last card. The
 * scrollbar takes a lane from the scrolled band only, which would centre it a
 * few pixels left of the bands above; every band therefore reserves that lane
 * (`scrollbar-gutter`, which needs an overflow other than visible), and where
 * scrollbars overlay the page instead, none does.
 *
 * Below that the whole page is one scroller (`GALLERY_PAGE`), so every band
 * sits inside the same lane and no band reserves one of its own.
 */
export const GALLERY_FRAME = 'px-4 md:px-8 md:[scrollbar-gutter:stable]';

/**
 * Everything under the titlebar. Below a laptop's width it is the one
 * scroller, its lane kept whether or not the page overflows, and matched by
 * one on the other side so a narrow window with a real scrollbar keeps the
 * column centred (an overlay scrollbar reserves neither). From there up it
 * only stacks the fixed bands over the grid's own scroller.
 */
export const GALLERY_PAGE = 'flex-1 min-h-0 overflow-y-auto max-md:[scrollbar-gutter:stable_both-edges] md:flex md:flex-col md:overflow-hidden';

/**
 * The column itself: wide enough to fill a laptop, capped so a line on an
 * ultrawide monitor stays one the eye can follow.
 */
export const GALLERY_COLUMN = 'mx-auto w-full max-w-[1440px]';
