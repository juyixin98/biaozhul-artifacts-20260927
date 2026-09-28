# Synthetic snapshot fixtures

Deterministic, fully fake repository used by the tests and verification
scripts. **Nothing here is a real credential** and no value is ever checked
against a network.

| Path | Purpose |
|---|---|
| `src/demo_app.py` | fake GitHub classic PAT, fake Slack bot token; also a `changeme` placeholder and a short low-entropy password that must NOT be flagged |
| `config/aws-credentials.ini` | AWS-documented *example* access key id/secret (shape only) |
| `config/service.env` | generic assigned token covered by `fixtures/baseline.toml` (content-fingerprint exemption) |
| `config/legacy-latin1.cfg` | valid latin-1, invalid UTF-8 file with an embedded token — exercises binary printable-run scanning |
| `firmware/device.bin` | NUL-containing binary blob with an embedded fake token |
| `keys/legacy_test.pem` | synthetic PEM private key block (hand-made body, no key pair) |
| `docs/notes.md`, `docs/blobhash.txt` | ordinary high-entropy-looking text WITHOUT secret-like assignment — entropy alone must not flag them |
| `node_modules/pkg/index.js` | vendored tree excluded by the default scope; its seeded fake token must never appear in results |
| `src/link-to-aws.ini` | symlink; the scanner must not follow it (points into `config/`) |
