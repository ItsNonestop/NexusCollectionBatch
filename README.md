# NexusCollectionBatch

`NexusCollectionBatch` is a user-facing Nexus collection downloader/installer built on a proven browser-first baseline.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- Brave or Chrome installed
- A Nexus Mods account logged in through your normal browser profile

## Install

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

## Start Browser CDP (manual option)

Start Brave with remote debugging:

```powershell
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
```

Then optionally verify:

```powershell
Invoke-WebRequest "http://127.0.0.1:9222/json/version" | Select-Object -ExpandProperty Content
```

If CDP is not running, `NexusCollectionBatch` will also try launching Brave/Chrome automatically.

## Run

Guided mode:

```powershell
python .\nexus_collection_batch.py
```

Batch launcher:

```powershell
.\run_nexus_collection_batch.cmd
```

The guided flow asks for:

- Collection URL (example: `https://www.nexusmods.com/games/<game>/collections/<slug>/mods`)
- Downloads folder
- Install folder (mods target path)

Saved defaults are stored in `nexus_collection_batch_config.json`.

## Non-interactive run

```powershell
python .\nexus_collection_batch.py --no-prompt --collection-url "https://www.nexusmods.com/games/<game>/collections/<slug>/mods" --downloads-dir "C:\Downloads\Nexus" --install-dir "D:\Games\<Game>\Mods"
```

Useful flags:

```powershell
python .\nexus_collection_batch.py --dry-run --max-mods 5
python .\nexus_collection_batch.py --skip-install
python .\nexus_collection_batch.py --cdp-url "http://127.0.0.1:9222"
```

## Output

- Run logs:
  - `logs/nexus-collection-batch-<timestamp>.json`
  - `logs/nexus-collection-batch-<timestamp>.txt`
- Install log:
  - `logs/nexus-collection-batch-install-<timestamp>.json`
- Install staging folder:
  - `logs/nexus-collection-batch-install-<timestamp>/`

## Notes

- Direct-session HTTP download may still fail on some machines (SSL/403). Click-flow fallback remains the stable baseline.
- If queue extraction is zero, diagnostics artifacts are written to `logs/`.
- Technical baseline reference is in `refs/auto-que-working-reference/`.
