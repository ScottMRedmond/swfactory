// The runner side of the runner contract: validators built from the JSON
// Schemas in contracts/, which are the only source of truth.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";

const CONTRACTS = join(dirname(fileURLToPath(import.meta.url)), "..", "contracts");

// null when the data is valid, otherwise the errors as one line of text.
type Validate = (data: unknown) => string | null;

export type Contract = {
  version: number;
  validateDispatch: Validate;
  validateResult: Validate;
  validateEnrolment: Validate;
  // A task's output (the agent's output.json) against its sub-schema of
  // result.schema.json.
  validateOutput: (task: string, data: unknown) => string | null;
  // That sub-schema as one self-contained JSON Schema, to show the agent.
  outputSchema: (task: string) => object;
};

const outputDef = (task: string) => `${task.replace(/-/g, "_")}_output`;

// A def from one of the schemas with every $ref it needs copied in under
// $defs, so it reads on its own. A ref into another schema file becomes a
// local def named <file>_<def>.
const selfContained = (schemas: Record<string, any>, root: string, def: string): object => {
  const defs: Record<string, unknown> = {};
  const rewrite = (node: any, from: string): any => {
    if (Array.isArray(node)) return node.map((n) => rewrite(n, from));
    if (!node || typeof node !== "object") return node;
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(node)) {
      if (key === "$ref" && typeof value === "string") {
        const [file, pointer = ""] = value.split("#");
        const owner = file ? file.replace(".schema.json", "") : from;
        const name = pointer.replace("/$defs/", "");
        const local = owner === root ? name : `${owner}_${name}`;
        if (!(local in defs)) {
          defs[local] = null;
          defs[local] = rewrite(schemas[owner].$defs[name], owner);
        }
        out.$ref = `#/$defs/${local}`;
      } else {
        out[key] = rewrite(value, from);
      }
    }
    return out;
  };
  const schema = rewrite(schemas[root].$defs[def], root);
  return Object.keys(defs).length ? { ...schema, $defs: defs } : schema;
};

const describe = (validate: ValidateFunction): Validate => (data) =>
  validate(data)
    ? null
    : (validate.errors ?? [])
        .map((e) => `${e.instancePath || "/"} ${e.message}`)
        .join("; ");

// Loads the schemas from a contracts folder (the repo's own by default).
// They $ref each other by file name, resolved against their $id, so they're
// all registered together.
export const loadContract = (dir = CONTRACTS): Contract => {
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  const schemas: Record<string, { $id: string; $defs?: any }> = {};
  for (const file of readdirSync(dir).filter((f) => f.endsWith(".schema.json"))) {
    const schema = JSON.parse(readFileSync(join(dir, file), "utf8"));
    schemas[file.replace(".schema.json", "")] = schema;
    ajv.addSchema(schema);
  }
  const compile = (name: string) => {
    const schema = schemas[name];
    if (!schema) throw new Error(`no ${name}.schema.json in ${dir}`);
    return describe(ajv.getSchema(schema.$id)!);
  };
  return {
    version: schemas.dispatch?.$defs?.contract_version?.const,
    validateDispatch: compile("dispatch"),
    validateResult: compile("result"),
    validateEnrolment: compile("enrolment"),
    validateOutput: (task, data) => {
      const validate = ajv.getSchema(`${schemas.result?.$id}#/$defs/${outputDef(task)}`);
      if (!validate) throw new Error(`no output sub-schema for ${task} in ${dir}`);
      return describe(validate)(data);
    },
    outputSchema: (task) => selfContained(schemas, "result", outputDef(task)),
  };
};

export type Contracts = {
  current: Contract;
  versions: number[];
  get: (version: unknown) => Contract | undefined;
};

// The versions a runner accepts: the current one and the one before it, so
// a merged schema change doesn't break an orchestrator that hasn't been
// redeployed yet. The previous version's schemas live in contracts/previous/
// (copied there when contract_version is bumped); while there's none, only
// the current version is accepted.
export const loadContracts = (dir = CONTRACTS): Contracts => {
  const current = loadContract(dir);
  const all = [current];
  const previousDir = join(dir, "previous");
  if (existsSync(previousDir)) {
    const previous = loadContract(previousDir);
    if (previous.version !== current.version - 1) {
      throw new Error(
        `contracts/previous/ is version ${previous.version}, not ${current.version - 1}`,
      );
    }
    all.push(previous);
  }
  return {
    current,
    versions: all.map((c) => c.version),
    get: (version) => all.find((c) => c.version === version),
  };
};
