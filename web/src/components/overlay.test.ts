import { afterEach, describe, expect, it, vi } from "vitest";
import { afterOverlay } from "./overlay";

/** Just enough of a browser window: a history entry and popstate. */
function fakeWindow(state: unknown) {
  const win = Object.assign(new EventTarget(), {
    history: { state },
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (t: ReturnType<typeof setTimeout>) => clearTimeout(t),
  });
  vi.stubGlobal("window", win);
  return win;
}

describe("afterOverlay", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

  it("goes at once when no overlay entry is open", () => {
    fakeWindow({ idx: 3 });
    const then = vi.fn();
    afterOverlay(then);
    expect(then).toHaveBeenCalledOnce();
  });

  it("waits until the overlay's entry is popped, so the link replaces the page's own entry", () => {
    const win = fakeWindow({ idx: 3, plexbieOverlay: 1 });
    const then = vi.fn();
    afterOverlay(then);
    expect(then).not.toHaveBeenCalled();
    win.dispatchEvent(new Event("popstate"));
    expect(then).toHaveBeenCalledOnce();
    // Only once, however many Backs follow.
    win.dispatchEvent(new Event("popstate"));
    expect(then).toHaveBeenCalledOnce();
  });

  it("still goes if Back never comes", () => {
    vi.useFakeTimers();
    fakeWindow({ plexbieOverlay: 1 });
    const then = vi.fn();
    afterOverlay(then);
    vi.advanceTimersByTime(499);
    expect(then).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(then).toHaveBeenCalledOnce();
  });
});
