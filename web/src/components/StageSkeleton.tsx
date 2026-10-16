/** Home's channel and ticker while they load, in their loaded shape (the strap, three lines'
 *  room for the title, the rail of titles, the 40px ticker), so nothing below moves when they arrive.
 *  Also Home's loading page, before the page's own code is here. */
export function StageSkeleton({ ticker = true }: { ticker?: boolean }) {
  return (
    <>
      <section className="stage stage--compact stage--bare" aria-busy="true" aria-label="Just arrived on Plex">
        <div className="shell stage__inner">
          <div className="stage__feature">
            <div className="stage__strapline"><div className="skeleton stage__strap-skeleton" /></div>
            <span className="stage__title stage__title--skeleton skeleton" aria-hidden>{"\u00a0"}</span>
          </div>
          <div className="stage__channels" aria-hidden><div className="skeleton stage__rail-skeleton" /></div>
        </div>
      </section>
      {ticker ? <div className="ticker" aria-hidden /> : null}
    </>
  );
}
