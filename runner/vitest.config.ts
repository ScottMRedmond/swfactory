import { defineConfig } from "vitest/config";

// The run script's tests drive git and Docker, so they run on their own
// (`npm run test:runner`), not with `npm test`.
export default defineConfig({
  test: {
    include: ["runner/**/*.test.ts"],
    testTimeout: 600_000,
    hookTimeout: 600_000,
  },
});
