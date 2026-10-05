// The owner's Plexbie logo, used as-is for the few small moments (empty,
// offline, request sent). Never redraw or alter it; the PNG is the brand mark.

export function Mascot({ label }: { label?: string }) {
  return (
    <img
      className="mascot"
      src="/brand/plexbie-192.png"
      width={84}
      height={84}
      alt={label ?? ""}
      aria-hidden={label ? undefined : true}
    />
  );
}
