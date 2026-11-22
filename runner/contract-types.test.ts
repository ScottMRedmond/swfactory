// Holds the runner's contract types (contract-types.ts) to the schemas in
// contracts/: their keys, which keys are required and their literal values.
// Needs neither git nor Docker.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { FAILURES, KEYS, OUTCOMES, PREVIOUS_OUTCOMES, REQUIRED, TASKS } from "./contract-types";
import { root } from "./testing";

const schema = (name: string) => JSON.parse(readFileSync(join(root, "contracts", `${name}.schema.json`), "utf8"));
const dispatch = schema("dispatch");
const result = schema("result");

const variants = TASKS.map((task) => dispatch.$defs[task]);
const keys = (object: { properties: object }) => Object.keys(object.properties).sort();
const sorted = (values: readonly string[]) => [...values].sort();

describe("the contract types", () => {
  it("cover each task's dispatch: every field any task has, and the ones all require", () => {
    expect(sorted(TASKS)).toEqual(sorted(dispatch.oneOf.map((v: { $ref: string }) => v.$ref.split("/").at(-1))));
    expect(variants.map((v) => v.properties.task.const)).toEqual([...TASKS]);
    expect(sorted(KEYS.dispatch)).toEqual([...new Set(variants.flatMap(keys))].sort());
    const required = variants.map((v) => v.required as string[]);
    expect(sorted(REQUIRED.dispatch)).toEqual(sorted(required[0]!.filter((k) => required.every((r) => r.includes(k)))));
  });

  it("match result.json", () => {
    expect(sorted(KEYS.result)).toEqual(keys(result));
    expect(sorted(REQUIRED.result)).toEqual(sorted(result.required));
    expect(sorted(TASKS)).toEqual(sorted(result.properties.task.enum));
    expect(sorted(OUTCOMES)).toEqual(sorted(result.properties.outcome.enum));
    expect(sorted(FAILURES)).toEqual(sorted(result.$defs.failure.enum));
  });

  it("match the shared parts", () => {
    const parts = {
      check: result.$defs.check,
      previous_attempt: dispatch.$defs.previous_attempt,
      triggered_by: dispatch.$defs.triggered_by,
      older_issue: dispatch.$defs.older_issues.items,
    } as const;
    for (const [name, part] of Object.entries(parts)) {
      expect(sorted(KEYS[name as keyof typeof parts]), name).toEqual(keys(part));
      expect(sorted(REQUIRED[name as keyof typeof parts]), name).toEqual(sorted(part.required));
    }
    expect(sorted(PREVIOUS_OUTCOMES)).toEqual(sorted(dispatch.$defs.previous_attempt.properties.outcome.enum));
  });
});
