// The Actions side of the runner: run.yml and the caller workflow template.
// They can't run here, so these tests pin down what the design asks of them
// (see "Runners" in docs/software-factory.md and "Dispatching and collecting"
// in docs/orchestrator.md). The run script itself is tested in
// run-task.test.ts.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path: string) => readFileSync(join(root, path), "utf8");

const CALLER = "runner/templates/factory.yml";
const RUN = ".github/workflows/run.yml";

describe("the caller workflow template", () => {
  const text = read(CALLER);
  const workflow = parse(text);
  const job = Object.values<any>(workflow.jobs)[0];

  it("is short: about 10 lines of YAML", () => {
    const lines = text.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#"));
    expect(lines.length).toBeLessThanOrEqual(16);
  });

  it("takes the dispatch as one JSON input of workflow_dispatch", () => {
    expect(Object.keys(workflow.on)).toEqual(["workflow_dispatch"]);
    expect(Object.keys(workflow.on.workflow_dispatch.inputs)).toEqual(["dispatch"]);
  });

  it("can only read the repo's contents", () => {
    expect(workflow.permissions).toEqual({ "contents": "read" });
  });

  it("calls the factory's run.yml at main with the dispatch", () => {
    expect(Object.keys(workflow.jobs)).toHaveLength(1);
    expect(job.uses).toMatch(/^[^/]+\/[^/]+\/\.github\/workflows\/run\.yml@main$/);
    expect(job.with).toEqual({ dispatch: "${{ inputs.dispatch }}" });
  });

  it("passes the Claude token and no other secret", () => {
    expect(job.secrets).toEqual({
      CLAUDE_CODE_OAUTH_TOKEN: "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
    });
  });

  // A reusable workflow's own run-name is ignored, so the caller sets it.
  it("names each run after the factory run id", () => {
    expect(workflow["run-name"]).toContain("${{ fromJSON(inputs.dispatch).run_id }}");
  });
});

describe("run.yml", () => {
  const workflow = parse(read(RUN));
  const jobs = Object.values<any>(workflow.jobs);
  const job = jobs[0];
  const steps: any[] = job.steps;
  const scriptSteps = steps.filter((s) => s.run?.includes("runner/run-task.ts"));
  const scriptIndex = steps.indexOf(scriptSteps[0]);

  it("is a reusable workflow taking one dispatch input", () => {
    expect(Object.keys(workflow.on)).toEqual(["workflow_call"]);
    expect(workflow.on.workflow_call.inputs).toEqual({
      dispatch: expect.objectContaining({ type: "string", required: true }),
    });
  });

  it("has one job, read-only", () => {
    expect(jobs).toHaveLength(1);
    expect(workflow.permissions).toEqual({ contents: "read" });
  });

  it("times the job out at the smaller of the dispatch's timeout and 60", () => {
    expect(job["timeout-minutes"]).toBe(
      "${{ fromJSON(inputs.dispatch).timeout_minutes > 60 && 60 || fromJSON(inputs.dispatch).timeout_minutes }}",
    );
  });

  it("calls the run script once, with the dispatch from the environment", () => {
    expect(scriptSteps).toHaveLength(1);
    const step = scriptSteps[0];
    expect(step.env.DISPATCH).toBe("${{ inputs.dispatch }}");
    // The dispatch is never pasted into the shell script itself.
    expect(step.run).not.toContain("inputs.dispatch");
  });

  it("gives the run script the read-only token and the Actions layer cache", () => {
    const step = scriptSteps[0];
    expect(step.env.GITHUB_TOKEN).toBe("${{ github.token }}");
    expect(step.env.FACTORY_BUILD_CACHE).toBe("gha");
    const before = steps.slice(0, scriptIndex).map((s) => s.uses ?? "");
    expect(before).toContainEqual(expect.stringMatching(/^docker\/setup-buildx-action@/));
    expect(before).toContainEqual(expect.stringMatching(/^crazy-max\/ghaction-github-runtime@/));
  });

  it("gives the Claude token to the run script only, which passes it to the agent alone", () => {
    const withToken = steps.filter((s) => JSON.stringify(s).includes("CLAUDE_CODE_OAUTH_TOKEN"));
    expect(withToken).toEqual([scriptSteps[0]]);
    expect(scriptSteps[0].env.CLAUDE_CODE_OAUTH_TOKEN).toBe("${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}");
    expect(job.env ?? {}).not.toHaveProperty("CLAUDE_CODE_OAUTH_TOKEN");
  });

  it("sets the App's bot identity for the agent's commits", () => {
    expect(scriptSteps[0].env).toHaveProperty("FACTORY_BOT_NAME");
    expect(scriptSteps[0].env).toHaveProperty("FACTORY_BOT_EMAIL");
  });

  it("only prepares the job before the script: no step runs a task's own commands", () => {
    for (const step of steps.slice(0, scriptIndex)) {
      if (step.run) expect(step.run.trim()).toMatch(/^npm ci/);
    }
  });

  it("checks out code only without keeping credentials", () => {
    for (const step of steps.filter((s) => s.uses?.startsWith("actions/checkout@"))) {
      expect(step.with["persist-credentials"]).toBe(false);
    }
  });

  it("uploads the script's output whatever happened, kept for 7 days", () => {
    const after = steps.slice(scriptIndex + 1);
    expect(after).toHaveLength(1);
    const [upload] = after;
    expect(upload.uses).toMatch(/^actions\/upload-artifact@/);
    expect(upload.if).toBe("always()");
    expect(upload.with).toMatchObject({ name: "factory-run", "retention-days": 7 });
  });
});
