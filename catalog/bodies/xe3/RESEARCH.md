# X-E3 Emulator Research Notes

## Catalog Status

The X-E3 catalog data compiles into the native extension and resolves:

- `resolve_firmware('xe3-132')` → **SUCCESS**
- `resolve_configuration('xe3-132')` → **SUCCESS**

Catalog files:
- `catalog/bodies/xe3/body.json` — soc: xpp, 1 GiB DRAM
- `catalog/bodies/xe3/controls.json` — 73 located control events
- `catalog/firmware/xe3/1.32/firmware.json` — fw update 107 MB, boot ROM 64 KB
- `catalog/firmware/xe3/1.32/control-ids.generated.json` — stream-split control IDs
- `configurations/emulator/xe3-132/run.json` — run configuration
- `configurations/emulator/xe3-discovery/hardware-inputs.json` — hardware inputs

## Emulator Boot Blockers

`prepare_configuration('xe3-132')` fails with:
```
native X70 boot does not support loader-start configurations
```

### Root cause

The xe3-132 run configuration uses `"start": {"kind": "loader"}`.
The native emulator boot implementation only supports `"start": {"kind": "rom"}`.

### Additional gaps

Even if the start kind were changed to `"rom"`, the configuration would still fail with
`native X70 boot requires declared startup inputs` because xe3-132 has:

- `execution: null` — no execution policy (mode, engine, quantum, instructions_per_tick)
- `inputs: null` — no hardware input declarations (GPIO keys, ADC, power, MCU, USB, card)
- `scene: null` — no scene definition (live image, statistics)

### Emulator-wide context

Of 28 bundled run configurations, only **1** can pass `prepare_configuration`:

| Configuration | Start | Execution | Inputs | Result |
|---|---|---|---|---|
| x70-110-rec | rom | yes | yes | **OK** |
| x70-110-runtime | rom | yes | yes | unsupported selection |
| xe5-112 | loader | yes | yes | loader not supported |
| x100vi-131 | loader | yes | yes | loader not supported |
| xt5-431 | loader | yes | yes | loader not supported |
| All others | mixed | no | no | missing inputs or loader |

The emulator's native boot path is currently purpose-built for the X70 body.

### What's needed to boot the X-E3

1. **Loader-start support** in the native Rust boot code — or an alternative boot path
   that can work with the XPP SoC's startup sequence
2. **Execution policy** — mode (strict/discovery), engine (unicorn/hypervisor),
   quantum, and instructions-per-tick ratio for the XPP dual-core Cortex-R
3. **Hardware inputs** — GPIO key map, ADC channel values, power controller state,
   MCU interface packet, serial registers, USB/card presence
4. **Scene definition** — live image dimensions/stride, statistics destinations,
   frame period

### Extracted firmware streams

The firmware update has been split. Extracted streams exist at:
```
.tools/firmware/xe3-132/streams/
  stream-00230268.bin  (4,851,436 bytes)
  stream-00470268.bin  (7,241,728 bytes)
  stream-035426a8.bin  (4,851,436 bytes)
  stream-037826a8.bin  (7,266,304 bytes)
```

### Build notes (Windows)

The `_native.pyd` must be rebuilt after adding xe3 catalog data.
Cargo outputs the DLL as `target/release/_native.dll` (not `fujimod_native.dll`).
Copy it to `bindings/python/python/fujimod/_native.pyd`.

The `python_extension.py` verification step fails on Windows because
its subprocess doesn't add `C:\libjpeg-turbo64\bin` to the DLL search path.
Workaround: build with cargo directly and copy the DLL manually.
