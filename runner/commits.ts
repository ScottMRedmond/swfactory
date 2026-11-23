// The agent's commits: the branch it commits on, and the git bundle the run
// uploads. See "Implement" and "The runner contract" in
// docs/software-factory.md.
//
// Commits are authored by the App's bot identity, with a Co-Authored-By
// trailer naming the human who triggered the work at their GitHub noreply
// address. The agent commits however it likes; afterwards every commit on
// the branch is rewritten with that identity and trailer, so neither depends
// on the agent getting it right.
//
// The identity comes from the environment (set by run.yml):
//
//   FACTORY_BOT_NAME   the App's bot login, e.g. my-factory[bot]
//   FACTORY_BOT_EMAIL  its noreply address, <bot user id>+<login>@users.noreply.github.com

import { existsSync } from "node:fs";
import { join } from "node:path";
import { AGENT_DIR } from "./agent";
import type { TriggeredBy } from "./contract-types";
import { run } from "./spawn";

export const BUNDLE = "branch.bundle";

export type Identity = { name: string; email: string };

const log = (message: string) => console.log(`[factory] ${message}`);

// Runs a bash script in the workspace with positional arguments, so nothing
// from the dispatch is pasted into the script.
const bash = (container: string, workspace: string, env: Record<string, string>, script: string, args: string[]) =>
  run("docker", [
    "exec", "-w", workspace,
    ...Object.entries(env).flatMap(([k, v]) => ["-e", `${k}=${v}`]),
    container, "bash", "-c", `set -euo pipefail\n${script}`, "bash", ...args,
  ]);

// The bot identity from the environment, or an error saying what's missing.
export const botIdentity = (): Identity | string => {
  const name = process.env.FACTORY_BOT_NAME;
  const email = process.env.FACTORY_BOT_EMAIL;
  if (!name || !email) return "FACTORY_BOT_NAME and FACTORY_BOT_EMAIL must be set to commit";
  return { name, email };
};

export const coAuthor = (user: TriggeredBy) =>
  `Co-Authored-By: ${user.login} <${user.id}+${user.login}@users.noreply.github.com>`;

// Before the agent: the named branch at base_sha, checked out, with the bot
// as the repo's git identity so the agent's own commits work.
export const openBranch = (container: string, workspace: string, branch: string, identity: Identity) => {
  const result = bash(container, workspace, {}, `
git checkout -q -b "$1"
git config user.name "$2"
git config user.email "$3"
`, [branch, identity.name, identity.email]);
  if (result.status !== 0) throw new Error(`can't open branch ${branch}: ${result.output}`);
};

// After the agent, whatever happened: rewrite the commits on the branch
// since base_sha with the bot identity and the trailer, and pack them into
// <outDir>/branch.bundle with base_sha as its prerequisite. No commits, no
// bundle (git can't make an empty one). Returns how many commits it packed.
export const bundleBranch = (
  container: string,
  workspace: string,
  options: { branch: string; baseSha: string; identity: Identity; triggeredBy: TriggeredBy; outDir: string },
): number => {
  const { identity } = options;
  const env = {
    GIT_AUTHOR_NAME: identity.name,
    GIT_AUTHOR_EMAIL: identity.email,
    GIT_COMMITTER_NAME: identity.name,
    GIT_COMMITTER_EMAIL: identity.email,
  };
  const inContainer = `${AGENT_DIR}/${BUNDLE}`;
  const result = bash(container, workspace, env, `
branch="refs/heads/$1" base="$2" trailer="$3" bundle="$4"
rm -f "$bundle"
git rev-parse -q --verify "$branch" > /dev/null || { echo 0; exit 0; }
commits=$(git rev-list --reverse --topo-order "$base..$branch")
[ -n "$commits" ] || { echo 0; exit 0; }
declare -A new
count=0
for c in $commits; do
  parents=()
  for p in $(git rev-list --parents -n 1 "$c" | cut -d' ' -f2-); do
    parents+=(-p "\${new[$p]:-$p}")
  done
  message=$(git log -1 --format=%B "$c" | git interpret-trailers --if-exists addIfDifferent --trailer "$trailer")
  new[$c]=$(printf '%s\\n' "$message" | GIT_AUTHOR_DATE=$(git log -1 --format=%aI "$c") \\
    git commit-tree --no-gpg-sign "$c^{tree}" "\${parents[@]}")
  last=\${new[$c]}
  count=$((count + 1))
done
git update-ref "$branch" "$last"
git bundle create -q "$bundle" "$base..$branch" >&2
echo "$count"
`, [options.branch, options.baseSha, coAuthor(options.triggeredBy), inContainer]);
  if (result.status !== 0) throw new Error(`can't bundle ${options.branch}: ${result.output}`);
  const count = Number(result.stdout.trim());
  if (count > 0) {
    const copy = run("docker", ["cp", `${container}:${inContainer}`, join(options.outDir, BUNDLE)]);
    if (copy.status !== 0 || !existsSync(join(options.outDir, BUNDLE))) {
      throw new Error(`can't copy the bundle out: ${copy.stderr}`);
    }
  }
  log(`bundled ${count} commit(s) on ${options.branch}`);
  return count;
};
