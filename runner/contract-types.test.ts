// Holds the runner's generated contract types (contracts.generated.ts) to the
// schemas in contracts/: regenerating them must change nothing. Needs
// neither git nor Docker.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { generateContracts, generatedPath } from "./gen-contracts";

describe("the generated contract types", () => {
  it("are up to date with contracts/*.schema.json (run `npm run gen:contracts`)", async () => {
    expect(readFileSync(generatedPath, "utf8")).toBe(await generateContracts());
  });
});
