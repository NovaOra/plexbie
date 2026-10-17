import { describe, expect, it } from "vitest";
import { holdRoom, holdWords } from "./hold";

describe("a hold-to-confirm key's words", () => {
  it("keeps its verb once a press arms it", () => {
    expect(holdWords("Decline")).toEqual({ hold: "Hold to decline", armed: "Press again to decline" });
    expect(holdWords("Cancel").armed).toBe("Press again to cancel");
    expect(holdWords("Sign out others").armed).toBe("Press again to sign out others");
  });

  it("keeps room for its longest line, armed or not, and for what it says after a bail", () => {
    expect(holdRoom("Decline")).toEqual(["Hold to decline", "Press again to decline"]);
    expect(holdRoom("Import it", "Not imported.")).toEqual(["Hold to import it", "Press again to import it", "Not imported."]);
  });
});
