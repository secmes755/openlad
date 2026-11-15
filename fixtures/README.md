# Test fixtures

Sample documents used by `scripts/smoke.sh` and local development checks.
The default quick smoke uses `fixtures/smoke/manifest.json` and validates exact
answer terms. Run `scripts/smoke.sh fixtures/manifest.json` for the full public
PDF smoke; its documents and questions are defined in that manifest.

All documents here are **publicly available** resources — source links are
recorded below and files are pinned at a fixed version so checks stay
reproducible. No proprietary or internal documents are placed in this
directory.

| file | source | purpose |
|---|---|---|
| `smoke/release_notes.md` | Repository-authored generic fixture | fast exact-answer and cleanup regression |
| `Rockchip-RK3588-Datasheet-V1.9.pdf` | https://rockchips.net/wp-content/uploads/2025/03/Rockchip-RK3588-Datasheet-V1.9-20241212.pdf | datasheet verification (35 pages) |
| `SMIC-2025-Annual-Report.pdf` | https://static.cninfo.com.cn/finalpage/2026-03-27/1225037057.PDF | annual report verification (232 pages) |
