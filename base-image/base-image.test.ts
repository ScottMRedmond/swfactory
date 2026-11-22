// Tests the factory base image from the outside, through `docker run`.
// They need Docker, so they aren't part of `npm test`: run
// `npm run build:image && npm run test:image`. The publish workflow
// (.github/workflows/base-image.yml) runs them before it pushes.

import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const image = process.env.FACTORY_BASE_IMAGE ?? "factory-base:test";

const docker = (...args: string[]): string =>
  execFileSync("docker", args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });

// Runs a bash script in a fresh container and returns its output, throwing
// if it exits non-zero. It reads the output with `docker logs` after the
// container stops, because an attached `docker run` can drop output.
const sh = (script: string, img = image): string => {
  const id = docker("run", "-d", "--entrypoint", "bash", img, "-c", script).trim();
  try {
    const status = Number(docker("wait", id).trim());
    const logs = spawnSync("docker", ["logs", id], { encoding: "utf8" });
    const output = logs.stdout + logs.stderr;
    if (status !== 0) {
      throw new Error(`exited ${status}: ${script}\n${output}`);
    }
    return output;
  } finally {
    docker("rm", "-f", id);
  }
};

describe("the factory base image", () => {
  it.each([
    ["node", "node --version"],
    ["git", "git --version"],
    ["the Claude Code CLI", "claude --version"],
    ["Sandcastle", "sandcastle --version"],
    ["agent-browser", "agent-browser --version"],
  ])("has %s on the PATH", (_name, command) => {
    expect(() => sh(command)).not.toThrow();
  });

  it("has a browser agent-browser can drive, so tasks can take screenshots", () => {
    const out = sh(
      [
        "agent-browser open 'data:text/html,<title>factory</title>'",
        "agent-browser get title",
        "agent-browser screenshot /tmp/shot.png",
        "agent-browser close",
        "test -s /tmp/shot.png && echo screenshot-ok",
      ].join(" && "),
    );
    expect(out).toContain("factory");
    expect(out).toContain("screenshot-ok");
  });

  it("installs the shared skills as the enabled `factory` plugin", () => {
    const out = sh("claude plugin list --json");
    const plugins = JSON.parse(out) as { id: string; enabled: boolean }[];
    expect(plugins).toContainEqual(
      expect.objectContaining({ id: expect.stringMatching(/^factory@/), enabled: true }),
    );
  });

  it("puts every shared skill in the `factory` plugin, so it's called as factory:<skill>", () => {
    const out = sh("claude plugin details factory");
    const skillsLine = out.split("\n").find((line) => line.trim().startsWith("Skills ("));
    const skills = skillsLine?.replace(/^\s*Skills \(\d+\)\s*/, "").split(/,\s*/).sort();
    expect(skills).toEqual([
      "code-review",
      "implement",
      "implement-spec",
      "pr",
      "software-factory",
      "to-tickets",
    ]);
  });

  it("can be built FROM by an enrolled repo that adds its own toolchain", () => {
    const dir = mkdtempSync(join(tmpdir(), "enrolment-"));
    writeFileSync(
      join(dir, "Dockerfile"),
      [
        `FROM ${image}`,
        "USER root",
        "RUN apt-get update && apt-get install -y --no-install-recommends python3 sqlite3 && rm -rf /var/lib/apt/lists/*",
        "USER agent",
      ].join("\n"),
    );
    const enrolled = `${image}-enrolled`;
    docker("build", "-t", enrolled, dir);
    try {
      const out = sh(
        "whoami && python3 --version && sqlite3 --version && claude plugin details factory",
        enrolled,
      );
      expect(out).toContain("agent");
      expect(out).toContain("Python 3");
      expect(out).toMatch(/Skills \(\d+\).*to-tickets/);
    } finally {
      docker("rmi", "-f", enrolled);
    }
  });
});
