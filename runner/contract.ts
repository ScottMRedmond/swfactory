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
