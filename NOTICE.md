# Notice

This repository is a derivative open-source project built from the MIT-licensed [Microsoft VibeVoice](https://github.com/microsoft/VibeVoice) codebase.

What this project adds:

- a Codex Desktop watcher for active-chat speech
- a direct `codex exec --json` speech bridge
- Apple Silicon MLX worker integration
- macOS menu bar controls
- desktop speech modes such as `live_fast`, `english_full`, and `status_only`
- queue control and watcher shutdown helpers

What this repository intentionally does not include:

- local user session data
- personal paths, hostnames, or private machine identifiers
- `.codex-home/` runtime state
- generated menu bar build artifacts
- local logs and generated audio outputs

Upstream license information remains in [LICENSE](LICENSE).
