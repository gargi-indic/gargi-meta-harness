# Spike: Antigravity CLI (`agy`) in non-TTY mode (M5.2)

Status: TEMPLATE. Everything below is UNVERIFIED until the maintainer runs the probe with
the real `agy` and fills it in. Issue #53 stays open until then; M5.3 must not start first.

## How to run the probe

```
uv run python scripts/spikes/agy_probe.py --agy "$(command -v agy)" --i-understand-this-calls-the-agent
```

This spends real quota. Captures land in `tests/fixtures/antigravity/<scenario>.<mode>.raw`
plus `summary.json`. Review the captures for secrets before committing. Optional live test:
`AGY_LIVE=1 uv run pytest -m agy_live tests/adapters/test_agy_live.py`.

## Environment

- agy version: UNVERIFIED (fill in `agy --version`)
- OS / terminal: UNVERIFIED
- Date of run: UNVERIFIED

## Results matrix (hung / ok)

| scenario | pipes | pty | pty + --print-timeout |
|---|---|---|---|
| hello | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| readonly-plan | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| edit-file | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| denied-tool | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| resume | UNVERIFIED | UNVERIFIED | UNVERIFIED |

## Flags needed

- `-p` / `--output-format stream-json`: UNVERIFIED
- `--print-timeout`: UNVERIFIED (does it exist, default, does raising it avoid the hang?)
- `--conversation <id>`: UNVERIFIED
- Permission / auto-approve flags: UNVERIFIED

## Denied tools in the stream

UNVERIFIED. The synthetic fixtures assume a `denied_tool` event; real shape unknown.
Update `AGY_EVENT_MAP` in `harness/adapters/agy_stream.py` accordingly.

## Conversation ids

UNVERIFIED. Synthetic fixtures assume a `conversation` event carrying `conversation_id`.

## Auth and quota

UNVERIFIED (interactive login required? quota cost per run? headless auth options?).

## Recommended adapter approach (pty vs pipe)

UNVERIFIED. Hypothesis from the issue: pipes hang without a TTY, so a pty is needed.

## Open risks

- Real stream-json shapes may differ from the synthetic fixtures: UNVERIFIED
- pty output carries ANSI/CR noise (parser tolerates it): UNVERIFIED against real output
- Hang behaviour may depend on login state or version: UNVERIFIED
