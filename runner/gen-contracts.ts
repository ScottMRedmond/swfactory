// Generates the runner contract's TypeScript types from contracts/*.schema.json
// into contracts.generated.ts (ADR 0001). Run it with `npm run
// gen:contracts`; contract-types.test.ts fails while the committed file is
// stale. contract-types.ts derives the names the runner uses from it.

import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { compile, type JSONSchema } from "json-schema-to-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const contracts = join(here, "..", "contracts");
export const generatedPath = join(here, "contracts.generated.ts");

const banner = `// Generated from contracts/dispatch.schema.json and contracts/result.schema.json
// by runner/gen-contracts.ts. Do not edit: change the schemas and run
// \`npm run gen:contracts\`.
`;

// Both schemas under one root, so a part they share (result's failure in
// dispatch's previous_attempt) is declared once.
const root: JSONSchema = {
  title: "Contracts",
  type: "object",
  additionalProperties: false,
  required: ["dispatch", "result"],
  properties: {
    dispatch: { $ref: "dispatch.schema.json" },
    result: { $ref: "result.schema.json" },
  },
};

export const generateContracts = async (): Promise<string> =>
  banner + "\n" + (await compile(root, "Contracts", { cwd: contracts, bannerComment: "", additionalProperties: false }));

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(generatedPath, await generateContracts());
}
