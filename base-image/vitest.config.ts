import { defineConfig } from "vitest/config";

// The base image's tests drive Docker, so they run on their own
// (`npm run test:image`), not with `npm test`.
export default defineConfig({
  test: {
    include: ["base-image/**/*.test.ts"],
    testTimeout: 600_000,
    hookTimeout: 600_000,
  },
});
