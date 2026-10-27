# XHS research browser limit

`xhs-research-browser-limit.patch` preserves the deployed source changes from the separate Xiaohongshu checkout without adding its repository, credentials, or binaries here.

Baseline: Xiaohongshu source commit `27b1111` (local baseline based on upstream `xpzouying/xiaohongshu-mcp`). Apply from that source repository with `git apply /path/to/xhs-research-browser-limit.patch`.

The patch caps process-wide service browsers at three, bounds queue waits and search/detail contexts, and retains QR-login browser ownership until cleanup. The original deployment passed Go package compilation, browser-slot race tests, and a binary build. The handoff records deadline limitations; browser startup/cleanup is not guaranteed to finish within the action deadline.
